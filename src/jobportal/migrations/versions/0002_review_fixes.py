"""Soft source removal, detail-fetch backoff and application versioning.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01 16:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.add_column(sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("detail_failures", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("detail_retry_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.drop_column("version")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("detail_retry_at")
        batch_op.drop_column("detail_failures")
    with op.batch_alter_table("sources", schema=None) as batch_op:
        batch_op.drop_column("removed_at")
