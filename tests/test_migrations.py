from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from jobportal.db import get_engine
from jobportal.migrate import current_revision, upgrade
from jobportal.models import Base
from jobportal.settings import Settings


def test_migrations_build_the_schema_the_models_describe(settings: Settings) -> None:
    """The migration history and the models must never drift apart."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")

    upgrade()
    assert current_revision() == "0002"
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())

    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)
    assert differences == [], differences

    upgrade()  # running it again is a no-op
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
