"""Database engine and sessions (SQLite by default, Postgres in production)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from jobportal.settings import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    SQLite drops tzinfo; storing naive UTC and re-attaching it on the way out
    keeps comparisons between Python and database values safe on both engines.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given; use timezone-aware UTC values")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # The web app and the worker are separate processes on one file.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        if ":memory:" not in url:
            Path(url.split("///", 1)[-1]).parent.mkdir(parents=True, exist_ok=True)
        # hide_parameters: a failing statement is logged without its values,
        # which would otherwise put message bodies and contact details in the log.
        engine = create_engine(url, connect_args={"check_same_thread": False}, hide_parameters=True)
        _configure_sqlite(engine)
        return engine
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


def get_engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        _engine = make_engine(get_settings().resolved_database_url)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _factory is not None
    return _factory


def reset_engine() -> None:
    """Dispose the cached engine (tests point the app at fresh databases)."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that commits on success and rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create or upgrade the schema to the latest migration."""
    from jobportal.migrate import upgrade

    upgrade()
