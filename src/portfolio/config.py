"""Runtime configuration, read from the environment only.

No credential appears in this repository, and there is deliberately no default
DATABASE_URL and no fallback connection string — nothing to accidentally connect
to if configuration is missing.

Two supported credential sources, in precedence order:

1. DATABASE_URL — a complete connection URL. Used for local development and for
   any throwaway database. Simple, and the password lives in a gitignored .env.

2. DB_SECRET_ARN plus DB_HOST / DB_PORT / DB_NAME — the deployed path. RDS
   generates the master password and stores it in AWS Secrets Manager
   (`--manage-master-user-password`), and the EC2 instance role grants read
   access to that one secret. The password is therefore never typed by a human,
   never written to a file, and never present in this repository or in the
   instance's environment. This is the option the assignment's "no credentials"
   requirement is really asking for.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

try:  # optional in production, where the service manager sets real env vars
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:  # pragma: no cover
    pass

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when required configuration is absent, rather than failing later."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    pool_size: int
    max_overflow: int
    pool_recycle: int
    host: str
    port: int
    debug: bool

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            database_url=resolve_database_url(),
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            # RDS derives max_connections from instance memory:
            # LEAST(DBInstanceClassMemory/9531392, 5000). On db.t4g.micro (1 GiB)
            # that is roughly 112, so SQLAlchemy's stock 5+10 per process is
            # comfortable and needs no special tuning.
            pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
            max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
            # Recycle well inside any intermediary's idle timeout. RDS itself does
            # not aggressively close idle connections, but a NAT gateway or load
            # balancer between the app and the database will, and a silently dead
            # socket surfaces as a random failure after a quiet period rather than
            # as anything resembling a connection problem. pool_pre_ping is the
            # real safety net; this reduces how often it has to fire.
            pool_recycle=int(os.environ.get("DB_POOL_RECYCLE", "1800")),
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8050")),
            debug=os.environ.get("DEBUG", "false").lower() in {"1", "true", "yes"},
        )


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def resolve_database_url() -> str:
    """Assemble the connection URL from whichever source is configured."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url

    secret_arn = os.environ.get("DB_SECRET_ARN", "").strip()
    if secret_arn:
        return _url_from_secrets_manager(secret_arn)

    raise ConfigError(
        "No database configuration found. Set DATABASE_URL for local development "
        "(copy .env.example to .env), or set DB_SECRET_ARN with DB_HOST / DB_NAME "
        "for the deployed instance. There is no default and no fallback."
    )


def _url_from_secrets_manager(secret_arn: str) -> str:
    """Build a connection URL from an RDS-managed secret.

    boto3 is imported lazily so local development and the test suite never need
    it, and credentials are resolved through the instance role rather than any
    stored access key.
    """
    host = os.environ.get("DB_HOST", "").strip()
    name = os.environ.get("DB_NAME", "postgres").strip()
    port = os.environ.get("DB_PORT", "5432").strip()
    sslmode = os.environ.get("DB_SSLMODE", "require").strip()
    if not host:
        raise ConfigError("DB_SECRET_ARN is set but DB_HOST is missing.")

    try:
        import boto3
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ConfigError(
            "DB_SECRET_ARN is set but boto3 is not installed; "
            "install requirements.txt on the instance."
        ) from exc

    # Region must be explicit. boto3 does not fall back to instance metadata for
    # the region, and a systemd service inherits nothing from a shell — so on EC2
    # this raised a bare botocore NoRegionError from deep inside endpoint
    # resolution, with nothing to indicate that a single environment variable was
    # the whole problem.
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or ""
    ).strip()
    if not region:
        raise ConfigError(
            "DB_SECRET_ARN is set but no AWS region is configured. Set AWS_REGION "
            "(or AWS_DEFAULT_REGION) in the environment — boto3 does not infer it "
            "from instance metadata, and a systemd unit inherits nothing from a shell."
        )

    # Client construction can fail on its own (bad region, no endpoint), so it is
    # inside the guard too. Everything here becomes a ConfigError, which is what
    # the WSGI entry point catches to serve a 503 naming the cause rather than
    # crash-looping under gunicorn.
    try:
        client = boto3.client("secretsmanager", region_name=region)
        raw = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as configuration, not a stack trace
        raise ConfigError(
            f"Could not read the database secret from {region}. Check the instance "
            f"role grants secretsmanager:GetSecretValue on {secret_arn}. "
            f"({type(exc).__name__}: {exc})"
        ) from exc

    payload = json.loads(raw)
    try:
        user, password = payload["username"], payload["password"]
    except KeyError as exc:
        raise ConfigError(
            "Database secret does not look like an RDS-managed credential; "
            "expected 'username' and 'password' keys."
        ) from exc

    log.info("resolved database credentials from Secrets Manager")
    # Quote both: a generated password can contain '@', ':' or '/', any of which
    # would otherwise corrupt the URL and produce a baffling parse error.
    return (
        f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{name}?sslmode={sslmode}"
    )


def redact(url: str) -> str:
    """Mask the password in a connection URL so it is safe to log.

    Every code path that prints a connection target goes through this. Logs get
    shipped, pasted into tickets and screenshotted in debriefs.
    """
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host = rest.rsplit("@", 1)
    if ":" not in creds:
        # No password to hide. Returning "user:***" here would be worse than a
        # no-op: it implies a credential exists on a URL that carries none.
        return url
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
