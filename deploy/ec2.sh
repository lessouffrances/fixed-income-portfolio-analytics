#!/usr/bin/env bash
#
# Provision, inspect and tear down the EC2 host that serves the dashboard.
#
#   ./deploy/ec2.sh provision    IAM role, security group, instance, app
#   ./deploy/ec2.sh status       instance state and the public URL
#   ./deploy/ec2.sh url          just the URL, for scripting
#   ./deploy/ec2.sh logs         tail the app's journal over SSM (no SSH)
#   ./deploy/ec2.sh console      serial console output; SSM-free fallback
#   ./deploy/ec2.sh redeploy     pull latest code and restart, over SSM
#   ./deploy/ec2.sh teardown     delete the instance, security group and role
#
# Design notes, all defensible in a debrief:
#
#   * No inbound SSH, and no SSH key at all. Shell access is via SSM Session
#     Manager, which authenticates through IAM rather than a key file that has to
#     live somewhere and be rotated. Port 22 is never opened.
#
#   * The instance reads the database password from Secrets Manager at runtime
#     through its instance role. The password is therefore never in the repo,
#     never in user-data (which any process on the box can read from the metadata
#     service), and never on your laptop.
#
#   * The role's Secrets Manager grant is scoped to one secret ARN, not to
#     secretsmanager:* — the instance can read the database password and nothing
#     else in the account.
#
#   * gunicorn binds port 80 directly as a non-root user via
#     CAP_NET_BIND_SERVICE, rather than running as root or adding nginx purely to
#     forward one port. Fewer moving parts to explain and to patch.
#
# Prerequisite: ./deploy/rds.sh provision must have run, since this reads the
# database endpoint and secret ARN from the RDS instance.

set -euo pipefail

PROJECT="fixed-income"
INSTANCE_NAME="${INSTANCE_NAME:-${PROJECT}-app}"
DB_INSTANCE_ID="${DB_INSTANCE_ID:-${PROJECT}-db}"
ROLE_NAME="${ROLE_NAME:-${PROJECT}-app-role}"
PROFILE_NAME="${PROFILE_NAME:-${PROJECT}-app-profile}"
SG_NAME="${SG_NAME:-${PROJECT}-app-sg}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
APP_PORT="${APP_PORT:-80}"
REPO_URL="${REPO_URL:-https://github.com/lessouffrances/fixed-income-portfolio-analytics.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-east-1)}"
PROJECT_TAG="fixed-income-portfolio-analytics"

# Amazon Linux 2023, resolved at run time rather than pinned: a hardcoded AMI id
# is stale within weeks and silently region-specific.
#
# Resolved via ec2:DescribeImages rather than the public SSM parameter that AWS
# publishes for this. The SSM route is the more idiomatic one, but reading it
# needs ssm:GetParameters, which AmazonEC2FullAccess does not grant — so a user
# who can launch instances perfectly well would fail at looking up what to launch.
# DescribeImages needs only EC2 permissions the deployer already has.
AMI_NAME_PATTERN="${AMI_NAME_PATTERN:-al2023-ami-2023*-kernel-6.1-x86_64}"

resolve_ami() {
  aws ec2 describe-images --region "$REGION" \
    --owners amazon \
    --filters "Name=name,Values=$AMI_NAME_PATTERN" \
              "Name=state,Values=available" \
              "Name=architecture,Values=x86_64" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text
}

log()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require_aws() {
  command -v aws >/dev/null 2>&1 || die "aws CLI not found. brew install awscli"
  aws sts get-caller-identity >/dev/null 2>&1 \
    || die "aws CLI is not authenticated. Run: aws configure"
}

instance_id() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$INSTANCE_NAME" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || echo "None"
}

sg_id() {
  aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None"
}

db_field() {
  aws rds describe-db-instances --region "$REGION" \
    --db-instance-identifier "$DB_INSTANCE_ID" \
    --query "DBInstances[0].$1" --output text 2>/dev/null || echo "None"
}

# ---------------------------------------------------------------------------

cmd_provision() {
  require_aws

  # --- database must exist first -------------------------------------------
  local db_host db_port db_name db_secret
  db_host="$(db_field 'Endpoint.Address')"
  [[ "$db_host" == "None" ]] && die \
    "RDS instance '$DB_INSTANCE_ID' not found. Run ./deploy/rds.sh provision first."
  db_port="$(db_field 'Endpoint.Port')"
  db_name="$(db_field 'DBName')"
  db_secret="$(db_field 'MasterUserSecret.SecretArn')"
  [[ "$db_secret" == "None" ]] && die \
    "RDS instance has no managed master secret; expected --manage-master-user-password."
  log "database $db_host:$db_port/$db_name"

  # --- IAM role and instance profile ---------------------------------------
  if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    log "creating IAM role $ROLE_NAME"
    aws iam create-role --role-name "$ROLE_NAME" \
      --assume-role-policy-document '{
        "Version":"2012-10-17",
        "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
      }' \
      --tags "Key=project,Value=$PROJECT_TAG" >/dev/null 2>&1 || die \
      "could not create the role. Your IAM user probably lacks iam:CreateRole — see deploy/iam-policy.json"

    # SSM agent access, so we get a shell without opening port 22.
    aws iam attach-role-policy --role-name "$ROLE_NAME" \
      --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null

    # Read exactly one secret, and nothing else.
    aws iam put-role-policy --role-name "$ROLE_NAME" \
      --policy-name read-db-secret \
      --policy-document "{
        \"Version\":\"2012-10-17\",
        \"Statement\":[{
          \"Effect\":\"Allow\",
          \"Action\":\"secretsmanager:GetSecretValue\",
          \"Resource\":\"$db_secret\"
        }]
      }" >/dev/null
  else
    log "reusing IAM role $ROLE_NAME"
  fi

  if ! aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
    log "creating instance profile $PROFILE_NAME"
    aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null
    aws iam add-role-to-instance-profile \
      --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME" >/dev/null
    # IAM is eventually consistent; a freshly created profile is briefly
    # unusable by RunInstances and the error does not say so.
    log "waiting for the instance profile to propagate"
    sleep 12
  else
    log "reusing instance profile $PROFILE_NAME"
  fi

  # --- security group: HTTP in, nothing else -------------------------------
  local vpc sg
  vpc="$(aws ec2 describe-vpcs --region "$REGION" \
    --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
  sg="$(sg_id)"
  if [[ "$sg" == "None" || -z "$sg" ]]; then
    log "creating security group $SG_NAME"
    sg="$(aws ec2 create-security-group --region "$REGION" \
      --group-name "$SG_NAME" --description "HTTP access for $PROJECT_TAG" \
      --vpc-id "$vpc" \
      --tag-specifications "ResourceType=security-group,Tags=[{Key=project,Value=$PROJECT_TAG}]" \
      --query 'GroupId' --output text)"
    # The assignment wants a public URL, so port 80 is open deliberately. Port 22
    # is not opened at all — shell access goes through SSM.
    aws ec2 authorize-security-group-ingress --region "$REGION" \
      --group-id "$sg" --protocol tcp --port "$APP_PORT" --cidr 0.0.0.0/0 >/dev/null
  else
    log "reusing security group $SG_NAME ($sg)"
  fi

  # --- let the app reach the database --------------------------------------
  local db_sg
  db_sg="$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=group-name,Values=${PROJECT}-db-sg" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")"
  if [[ "$db_sg" != "None" && -n "$db_sg" ]]; then
    log "authorising Postgres from the app security group"
    aws ec2 authorize-security-group-ingress --region "$REGION" \
      --group-id "$db_sg" --protocol tcp --port 5432 \
      --source-group "$sg" >/dev/null 2>&1 || log "  (rule already present)"
  else
    warn "database security group ${PROJECT}-db-sg not found; the app may not reach the database"
  fi

  # --- launch ---------------------------------------------------------------
  local existing; existing="$(instance_id)"
  if [[ "$existing" != "None" && -n "$existing" ]]; then
    log "instance $existing already exists; skipping launch"
  else
    local ami; ami="$(resolve_ami)"
    [[ -z "$ami" || "$ami" == "None" ]] && die \
      "could not resolve an Amazon Linux 2023 AMI in $REGION matching '$AMI_NAME_PATTERN'"
    log "launching $INSTANCE_TYPE from $ami"

    local userdata; userdata="$(mktemp)"
    cat > "$userdata" <<USERDATA
#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/app-bootstrap.log) 2>&1

dnf -y update
dnf -y install git python3.11 python3.11-pip

useradd --system --create-home --shell /sbin/nologin app || true
install -d -o app -g app /opt/app

sudo -u app git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" /opt/app/repo
cd /opt/app/repo

sudo -u app python3.11 -m venv /opt/app/venv
sudo -u app /opt/app/venv/bin/pip install --upgrade pip
sudo -u app /opt/app/venv/bin/pip install -e ".[serve]"

# Configuration only. The database password is NOT here — the app resolves it
# from Secrets Manager at runtime via the instance role, so nothing secret is
# ever written to disk or to user-data.
cat > /opt/app/env <<ENVFILE
DB_SECRET_ARN=$db_secret
DB_HOST=$db_host
DB_PORT=$db_port
DB_NAME=$db_name
DB_SSLMODE=require
DATA_DIR=/opt/app/repo/data
HOST=0.0.0.0
PORT=$APP_PORT
DEBUG=false
ENVFILE
chmod 600 /opt/app/env
chown app:app /opt/app/env

# One-shot load. The extracts are a complete snapshot and the loader is a full
# refresh, so re-running is safe and idempotent.
cat > /etc/systemd/system/app-load.service <<'UNIT'
[Unit]
Description=Load the portfolio extracts into the database
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=app
WorkingDirectory=/opt/app/repo
EnvironmentFile=/opt/app/env
ExecStart=/opt/app/venv/bin/portfolio-load
RemainAfterExit=yes
UNIT

cat > /etc/systemd/system/app.service <<'UNIT'
[Unit]
Description=Fixed-income portfolio analytics dashboard
After=app-load.service
Requires=app-load.service

[Service]
User=app
WorkingDirectory=/opt/app/repo
EnvironmentFile=/opt/app/env
# Bind port 80 as a non-root user rather than running gunicorn as root or adding
# nginx solely to forward a port.
AmbientCapabilities=CAP_NET_BIND_SERVICE
ExecStart=/opt/app/venv/bin/gunicorn portfolio.app.main:server \\
  --bind 0.0.0.0:${APP_PORT} --workers 2 --timeout 60 --access-logfile -
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now app-load.service
systemctl enable --now app.service
USERDATA

    aws ec2 run-instances --region "$REGION" \
      --image-id "$ami" \
      --instance-type "$INSTANCE_TYPE" \
      --iam-instance-profile "Name=$PROFILE_NAME" \
      --security-group-ids "$sg" \
      --user-data "file://$userdata" \
      --metadata-options "HttpTokens=required" \
      --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":16,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
      --tag-specifications \
        "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME},{Key=project,Value=$PROJECT_TAG}]" \
      --query 'Instances[0].InstanceId' --output text >/dev/null
    rm -f "$userdata"

    log "waiting for the instance to run"
    aws ec2 wait instance-running --region "$REGION" \
      --filters "Name=tag:Name,Values=$INSTANCE_NAME"
  fi

  log "instance is up; bootstrap takes a further 2-4 minutes"
  cmd_status
  echo
  log "poll until it answers:"
  echo "    until curl -sf \$(./deploy/ec2.sh url)/healthz; do sleep 15; done"
}

cmd_status() {
  require_aws
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$INSTANCE_NAME" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[0].Instances[0].{
        state:State.Name,
        id:InstanceId,
        type:InstanceType,
        publicIp:PublicIpAddress,
        privateIp:PrivateIpAddress,
        profile:IamInstanceProfile.Arn
      }' --output table
}

cmd_url() {
  require_aws
  local ip; ip="$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$INSTANCE_NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
  [[ "$ip" == "None" || -z "$ip" ]] && die "no running instance found"
  if [[ "$APP_PORT" == "80" ]]; then echo "http://$ip"; else echo "http://$ip:$APP_PORT"; fi
}

_ssm_run() {
  local id; id="$(instance_id)"
  [[ "$id" == "None" ]] && die "no instance found"
  local cmd; cmd="$(aws ssm send-command --region "$REGION" \
    --instance-ids "$id" --document-name "AWS-RunShellScript" \
    --parameters "commands=[\"$1\"]" \
    --query 'Command.CommandId' --output text)"
  sleep 6
  aws ssm get-command-invocation --region "$REGION" \
    --command-id "$cmd" --instance-id "$id" \
    --query '{status:Status,out:StandardOutputContent,err:StandardErrorContent}' \
    --output text
}

cmd_logs() {
  require_aws
  log "app service journal (via SSM, no SSH)"
  _ssm_run "journalctl -u app.service -u app-load.service -n 60 --no-pager"
}

cmd_console() {
  # SSM-free fallback. The serial console carries cloud-init and user-data output,
  # which is where a failed bootstrap shows up, and reading it needs only
  # ec2:GetConsoleOutput — granted by AmazonEC2FullAccess. Use this when SSM
  # permissions are not in place, or when the instance never got far enough to
  # register with SSM at all (which is exactly when bootstrap failed).
  require_aws
  local id; id="$(instance_id)"
  [[ "$id" == "None" ]] && die "no instance found"
  log "serial console output for $id (bootstrap log lives here)"
  aws ec2 get-console-output --region "$REGION" --instance-id "$id" \
    --query 'Output' --output text 2>/dev/null | tail -80 \
    || warn "no console output yet; it can take 3-4 minutes to appear after launch"
}

cmd_redeploy() {
  require_aws
  log "pulling latest $REPO_BRANCH and restarting"
  _ssm_run "cd /opt/app/repo && sudo -u app git pull && sudo -u app /opt/app/venv/bin/pip install -e '.[serve]' && systemctl restart app-load.service app.service && systemctl is-active app.service"
}

cmd_teardown() {
  require_aws
  warn "this deletes the EC2 instance, its security group, role and instance profile"
  read -r -p "type the instance name to confirm: " confirm
  [[ "$confirm" == "$INSTANCE_NAME" ]] || die "confirmation did not match; nothing deleted"

  local id; id="$(instance_id)"
  if [[ "$id" != "None" && -n "$id" ]]; then
    log "terminating $id"
    aws ec2 terminate-instances --region "$REGION" --instance-ids "$id" >/dev/null
    aws ec2 wait instance-terminated --region "$REGION" --instance-ids "$id"
  fi

  # The security group cannot go until the ENI is released, which lags termination.
  local sg; sg="$(sg_id)"
  if [[ "$sg" != "None" && -n "$sg" ]]; then
    log "deleting security group $sg"
    for _ in 1 2 3 4 5 6; do
      aws ec2 delete-security-group --region "$REGION" --group-id "$sg" 2>/dev/null && break
      sleep 10
    done || warn "could not delete $sg; retry in a minute"
  fi

  if aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
    log "deleting instance profile $PROFILE_NAME"
    aws iam remove-role-from-instance-profile \
      --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME" >/dev/null 2>&1 || true
    aws iam delete-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null
  fi

  if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    log "deleting role $ROLE_NAME"
    aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name read-db-secret >/dev/null 2>&1 || true
    aws iam detach-role-policy --role-name "$ROLE_NAME" \
      --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore >/dev/null 2>&1 || true
    aws iam delete-role --role-name "$ROLE_NAME" >/dev/null
  fi

  log "EC2 teardown complete. The database is separate: ./deploy/rds.sh teardown"
}

case "${1:-}" in
  provision) cmd_provision ;;
  status)    cmd_status ;;
  url)       cmd_url ;;
  logs)      cmd_logs ;;
  console)   cmd_console ;;
  redeploy)  cmd_redeploy ;;
  teardown)  cmd_teardown ;;
  *)
    sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
