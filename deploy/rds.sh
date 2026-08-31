#!/usr/bin/env bash
#
# Provision, inspect and tear down the RDS PostgreSQL instance.
#
#   ./deploy/rds.sh provision     create the security group and the instance
#   ./deploy/rds.sh status        endpoint, state, and the secret ARN
#   ./deploy/rds.sh env           print the environment block for the EC2 host
#   ./deploy/rds.sh teardown      delete the instance and its security group
#
# Design notes, all of them defensible in a debrief:
#
#   * --manage-master-user-password. RDS generates the master password and stores
#     it in Secrets Manager. No human ever sees it, it is never typed into a
#     shell, never lands in shell history, and cannot reach this repository. The
#     EC2 instance role is granted read access to that one secret.
#
#   * --no-publicly-accessible by default. The database has no route from the
#     internet; the application reaches it over private VPC networking. This is
#     the single biggest security difference from a hosted database with a public
#     endpoint, and it costs nothing.
#
#   * The engine version is deliberately not pinned. Pinning a minor version in a
#     script guarantees a confusing failure the month AWS retires it; RDS's
#     default for the major version is a better target. Override with ENGINE_VERSION.
#
# This script never prints a password and never accepts one as an argument.

set -euo pipefail

INSTANCE_ID="${INSTANCE_ID:-fixed-income-db}"
DB_NAME="${DB_NAME:-portfolio}"
MASTER_USER="${MASTER_USER:-dbadmin}"
INSTANCE_CLASS="${INSTANCE_CLASS:-db.t4g.micro}"
STORAGE_GB="${STORAGE_GB:-20}"
SG_NAME="${SG_NAME:-fixed-income-db-sg}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-east-1)}"
PROJECT_TAG="fixed-income-portfolio-analytics"

# Optional ingress. Exactly one is expected, and neither is set by default:
#   ALLOW_SG=sg-0123...     allow Postgres from that security group (the EC2 host)
#   ALLOW_CIDR=1.2.3.4/32   allow Postgres from an address (needs PUBLIC=yes)
ALLOW_SG="${ALLOW_SG:-}"
ALLOW_CIDR="${ALLOW_CIDR:-}"
PUBLIC="${PUBLIC:-no}"

log()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require_aws() {
  command -v aws >/dev/null 2>&1 || die "aws CLI not found. brew install awscli"
  aws sts get-caller-identity >/dev/null 2>&1 \
    || die "aws CLI is not authenticated. Run: aws configure"
}

default_vpc() {
  aws ec2 describe-vpcs --region "$REGION" \
    --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text
}

sg_id() {
  aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None"
}

# ---------------------------------------------------------------------------

cmd_provision() {
  require_aws

  local vpc; vpc="$(default_vpc)"
  [[ "$vpc" == "None" || -z "$vpc" ]] && die "no default VPC in $REGION"
  log "region $REGION, default VPC $vpc"

  # --- security group -------------------------------------------------------
  local sg; sg="$(sg_id)"
  if [[ "$sg" == "None" || -z "$sg" ]]; then
    log "creating security group $SG_NAME"
    sg="$(aws ec2 create-security-group --region "$REGION" \
      --group-name "$SG_NAME" \
      --description "Postgres access for $PROJECT_TAG" \
      --vpc-id "$vpc" \
      --tag-specifications "ResourceType=security-group,Tags=[{Key=project,Value=$PROJECT_TAG}]" \
      --query 'GroupId' --output text)"
  else
    log "reusing security group $SG_NAME ($sg)"
  fi

  # Refuse to expose a database to the whole internet, even if asked. An
  # accidental 0.0.0.0/0 on port 5432 is the classic way these get compromised,
  # and the assignment does not need the database reachable from anywhere.
  if [[ "$ALLOW_CIDR" == "0.0.0.0/0" ]]; then
    die "refusing to open port 5432 to 0.0.0.0/0. Use ALLOW_SG with the EC2 security group instead."
  fi

  if [[ -n "$ALLOW_SG" ]]; then
    log "authorising Postgres from security group $ALLOW_SG"
    aws ec2 authorize-security-group-ingress --region "$REGION" \
      --group-id "$sg" --protocol tcp --port 5432 \
      --source-group "$ALLOW_SG" >/dev/null 2>&1 || warn "ingress rule already present"
  elif [[ -n "$ALLOW_CIDR" ]]; then
    log "authorising Postgres from $ALLOW_CIDR"
    aws ec2 authorize-security-group-ingress --region "$REGION" \
      --group-id "$sg" --protocol tcp --port 5432 \
      --cidr "$ALLOW_CIDR" >/dev/null 2>&1 || warn "ingress rule already present"
  else
    warn "no ingress rule created. Nothing can connect yet."
    warn "Re-run with ALLOW_SG=<ec2-security-group-id> once the EC2 host exists."
  fi

  # --- instance -------------------------------------------------------------
  if aws rds describe-db-instances --region "$REGION" \
       --db-instance-identifier "$INSTANCE_ID" >/dev/null 2>&1; then
    log "instance $INSTANCE_ID already exists; skipping creation"
  else
    log "creating $INSTANCE_ID ($INSTANCE_CLASS, ${STORAGE_GB}GB gp3)"
    local public_flag="--no-publicly-accessible"
    if [[ "$PUBLIC" == "yes" ]]; then
      warn "creating a PUBLICLY ACCESSIBLE database because PUBLIC=yes"
      warn "prefer running the loader from EC2 over exposing the database"
      public_flag="--publicly-accessible"
    fi

    # shellcheck disable=SC2086
    aws rds create-db-instance --region "$REGION" \
      --db-instance-identifier "$INSTANCE_ID" \
      --db-instance-class "$INSTANCE_CLASS" \
      --engine postgres \
      ${ENGINE_VERSION:+--engine-version "$ENGINE_VERSION"} \
      --allocated-storage "$STORAGE_GB" \
      --storage-type gp3 \
      --db-name "$DB_NAME" \
      --master-username "$MASTER_USER" \
      --manage-master-user-password \
      --vpc-security-group-ids "$sg" \
      $public_flag \
      --backup-retention-period 1 \
      --no-multi-az \
      --no-auto-minor-version-upgrade \
      --copy-tags-to-snapshot \
      --tags "Key=project,Value=$PROJECT_TAG" \
      --query 'DBInstance.DBInstanceIdentifier' --output text >/dev/null

    log "waiting for the instance to become available (typically 5-10 minutes)"
    aws rds wait db-instance-available --region "$REGION" \
      --db-instance-identifier "$INSTANCE_ID"
  fi

  log "provisioned"
  cmd_status
  echo
  cmd_env
}

cmd_status() {
  require_aws
  aws rds describe-db-instances --region "$REGION" \
    --db-instance-identifier "$INSTANCE_ID" \
    --query 'DBInstances[0].{
        state:DBInstanceStatus,
        endpoint:Endpoint.Address,
        port:Endpoint.Port,
        engine:EngineVersion,
        class:DBInstanceClass,
        public:PubliclyAccessible,
        database:DBName,
        secret:MasterUserSecret.SecretArn
      }' --output table
}

cmd_env() {
  require_aws
  local host port secret db
  host="$(aws rds describe-db-instances --region "$REGION" \
    --db-instance-identifier "$INSTANCE_ID" \
    --query 'DBInstances[0].Endpoint.Address' --output text)"
  port="$(aws rds describe-db-instances --region "$REGION" \
    --db-instance-identifier "$INSTANCE_ID" \
    --query 'DBInstances[0].Endpoint.Port' --output text)"
  db="$(aws rds describe-db-instances --region "$REGION" \
    --db-instance-identifier "$INSTANCE_ID" \
    --query 'DBInstances[0].DBName' --output text)"
  secret="$(aws rds describe-db-instances --region "$REGION" \
    --db-instance-identifier "$INSTANCE_ID" \
    --query 'DBInstances[0].MasterUserSecret.SecretArn' --output text)"

  cat <<EOF
# Environment for the EC2 host. Contains no credential: the password stays in
# Secrets Manager and is read at runtime through the instance role.
# Leave DATABASE_URL unset here, or it takes precedence over the secret.
DB_SECRET_ARN=$secret
DB_HOST=$host
DB_PORT=$port
DB_NAME=$db
DB_SSLMODE=require

# The instance role needs exactly this, and nothing broader:
#   Action:   secretsmanager:GetSecretValue
#   Resource: $secret
EOF
}

cmd_teardown() {
  require_aws
  warn "this permanently deletes $INSTANCE_ID and all its data"
  read -r -p "type the instance name to confirm: " confirm
  [[ "$confirm" == "$INSTANCE_ID" ]] || die "confirmation did not match; nothing deleted"

  log "deleting instance $INSTANCE_ID"
  aws rds delete-db-instance --region "$REGION" \
    --db-instance-identifier "$INSTANCE_ID" \
    --skip-final-snapshot \
    --delete-automated-backups >/dev/null

  log "waiting for deletion to complete"
  aws rds wait db-instance-deleted --region "$REGION" \
    --db-instance-identifier "$INSTANCE_ID"

  # The security group can only be removed once nothing references it.
  local sg; sg="$(sg_id)"
  if [[ "$sg" != "None" && -n "$sg" ]]; then
    log "deleting security group $SG_NAME ($sg)"
    aws ec2 delete-security-group --region "$REGION" --group-id "$sg" \
      || warn "could not delete $sg; something still references it"
  fi

  log "teardown complete. RDS-managed secrets are removed with the instance."
}

case "${1:-}" in
  provision) cmd_provision ;;
  status)    cmd_status ;;
  env)       cmd_env ;;
  teardown)  cmd_teardown ;;
  *)
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
