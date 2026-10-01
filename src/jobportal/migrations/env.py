"""Alembic environment for jobportal."""

from __future__ import annotations

from typing import Any

from alembic import context

from jobportal.db import UTCDateTime, make_engine
from jobportal.models import Base

config = context.config
target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Keep migrations self-contained: render our type decorators as plain SQLAlchemy types."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_item=render_item,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    assert url, "sqlalchemy.url is not configured"
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_item=render_item,
                # SQLite cannot ALTER most things; batch mode rewrites the table instead.
                render_as_batch=connection.dialect.name == "sqlite",
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
