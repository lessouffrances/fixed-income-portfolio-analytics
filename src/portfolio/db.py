"""Engine construction and schema creation."""

from __future__ import annotations

import logging

from sqlalchemy import Engine, create_engine, text

from .config import Settings, redact
from .models import metadata

log = logging.getLogger(__name__)


def make_engine(settings: Settings | None = None, *, url: str | None = None) -> Engine:
    """Build an engine.

    `url` is an explicit override used by the tests (SQLite) so they never need
    real configuration; production always goes through Settings.from_env().
    """
    if url is None:
        settings = settings or Settings.from_env()
        url = settings.database_url
        pool_kwargs = dict(
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            # Aiven closes idle connections server-side. Recycling below that
            # window avoids handing the app a socket the server has already
            # dropped, which otherwise appears as a random failure after a quiet
            # period rather than as a connection problem.
            pool_recycle=280,
            pool_pre_ping=True,
        )
    else:
        pool_kwargs = {}

    log.info("connecting to %s", redact(url))
    return create_engine(url, future=True, **pool_kwargs)


def create_schema(engine: Engine) -> None:
    """Create every table if absent. Idempotent, so it is safe on every boot."""
    metadata.create_all(engine)


def drop_schema(engine: Engine) -> None:
    """Drop every table. Used by the tests and by a deliberate --reset load."""
    metadata.drop_all(engine)


def healthcheck(engine: Engine) -> bool:
    """Cheap liveness probe for the app's status endpoint."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — a failed probe must report false, not raise
        log.exception("database healthcheck failed")
        return False
