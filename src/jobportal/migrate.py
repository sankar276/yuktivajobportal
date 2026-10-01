"""Schema migrations (Alembic), runnable without an ``alembic.ini``."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from jobportal.settings import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    resolved = url or get_settings().resolved_database_url
    # ConfigParser treats % as interpolation; URLs may contain escaped characters.
    config.set_main_option("sqlalchemy.url", resolved.replace("%", "%%"))
    return config


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def current_revision(url: str | None = None) -> str | None:
    from alembic.runtime.migration import MigrationContext

    from jobportal.db import make_engine

    engine = make_engine(url or get_settings().resolved_database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
