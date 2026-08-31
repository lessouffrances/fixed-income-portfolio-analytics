"""Runtime configuration, read from the environment only.

No credential appears in this repository. The database URL arrives from the
environment: a local .env file in development, and the EC2 instance environment
in production. The assignment forbids credentials in the repo, so there is
deliberately no default value for DATABASE_URL and no fallback connection string
to accidentally fall back to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # optional in production, where real env vars are set by the service manager
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:  # pragma: no cover
    pass


class ConfigError(RuntimeError):
    """Raised when required configuration is absent, rather than failing later."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    pool_size: int
    max_overflow: int
    host: str
    port: int
    debug: bool

    @staticmethod
    def from_env() -> "Settings":
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise ConfigError(
                "DATABASE_URL is not set. Copy .env.example to .env and fill in the "
                "Aiven service URI, or export DATABASE_URL in the environment."
            )
        return Settings(
            database_url=url,
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            # Aiven's free plan caps concurrent connections at roughly 20, shared
            # across the loader, the web app and any psql session. A default
            # SQLAlchemy pool (5 + 10 overflow) per process exhausts that quickly
            # and surfaces as connection errors that look unrelated to pooling.
            pool_size=int(os.environ.get("DB_POOL_SIZE", "3")),
            max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "2")),
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8050")),
            debug=os.environ.get("DEBUG", "false").lower() in {"1", "true", "yes"},
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
