"""Initial schema.

Revision ID: 0001
Revises: -
Create Date: 2026-09-30 22:53:50.454959
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column(
            "config",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("initialized", sa.Boolean(), nullable=False),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("jobs_open", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("last_modified", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "token", name="uq_sources_kind_token"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "answers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("question_key", sa.String(length=300), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "question_key", name="uq_answers_user_key"),
    )
    with op.batch_alter_table("answers", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_answers_user_id"), ["user_id"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("company_key", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("fingerprint", sa.String(length=32), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=False),
        sa.Column("remote", sa.Boolean(), nullable=True),
        sa.Column("workplace", sa.String(length=16), nullable=True),
        sa.Column("employment_type", sa.String(length=32), nullable=True),
        sa.Column("department", sa.String(length=300), nullable=False),
        sa.Column("requisition_id", sa.String(length=120), nullable=False),
        sa.Column("description_html", sa.Text(), nullable=False),
        sa.Column("description_text", sa.Text(), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("apply_url", sa.String(length=1000), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_backfill", sa.Boolean(), nullable=False),
        sa.Column("needs_detail", sa.Boolean(), nullable=False),
        sa.Column("comp_min", sa.Float(), nullable=True),
        sa.Column("comp_max", sa.Float(), nullable=True),
        sa.Column("comp_currency", sa.String(length=8), nullable=True),
        sa.Column("comp_period", sa.String(length=16), nullable=True),
        sa.Column("contact_name", sa.String(length=200), nullable=False),
        sa.Column("contact_email", sa.String(length=320), nullable=True),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column(
            "facts",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "raw",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_jobs_source_external"),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index("ix_jobs_closed", ["closed_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_company_key"), ["company_key"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_fingerprint"), ["fingerprint"], unique=False)
        batch_op.create_index("ix_jobs_first_seen", ["first_seen_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_source_id"), ["source_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_workplace"), ["workplace"], unique=False)

    op.create_table(
        "job_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lane", sa.String(length=64), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column(
            "reasons",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "breakdown",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("saved", sa.Boolean(), nullable=False),
        sa.Column("hidden", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "user_id", name="uq_scores_job_user"),
    )
    with op.batch_alter_table("job_scores", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_job_scores_decision"), ["decision"], unique=False)
        batch_op.create_index(batch_op.f("ix_job_scores_job_id"), ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_job_scores_user_id"), ["user_id"], unique=False)

    op.create_table(
        "resume_variants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("variant", sa.String(length=64), nullable=False),
        sa.Column("pdf_path", sa.String(length=1000), nullable=True),
        sa.Column("docx_path", sa.String(length=1000), nullable=True),
        sa.Column(
            "content",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "changes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "matched",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "gaps",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("resume_variants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_resume_variants_job_id"), ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_resume_variants_user_id"), ["user_id"], unique=False)

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("auto", sa.Boolean(), nullable=False),
        sa.Column("resume_variant_id", sa.Integer(), nullable=True),
        sa.Column(
            "prepared",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "blockers",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_action", sa.String(length=300), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("confirmation", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resume_variant_id"], ["resume_variants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "job_id", name="uq_applications_user_job"),
    )
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_applications_job_id"), ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_applications_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_applications_user_id"), ["user_id"], unique=False)

    op.create_table(
        "application_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column(
            "detail",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("application_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_application_events_application_id"), ["application_id"], unique=False
        )

    op.create_table(
        "inbound_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("in_reply_to", sa.String(length=255), nullable=False),
        sa.Column("from_addr", sa.String(length=320), nullable=False),
        sa.Column("from_name", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "message_id", name="uq_inbound_user_msg"),
    )
    with op.batch_alter_table("inbound_emails", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_inbound_emails_user_id"), ["user_id"], unique=False)

    op.create_table(
        "ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("client_name", sa.String(length=200), nullable=False),
        sa.Column("client_key", sa.String(length=200), nullable=False),
        sa.Column("role_title", sa.String(length=500), nullable=False),
        sa.Column("requisition_id", sa.String(length=120), nullable=False),
        sa.Column("vendor_name", sa.String(length=200), nullable=False),
        sa.Column("vendor_key", sa.String(length=200), nullable=False),
        sa.Column("vendor_contact", sa.String(length=320), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("engagement", sa.String(length=32), nullable=False),
        sa.Column("rate", sa.String(length=120), nullable=False),
        sa.Column("resume_variant_id", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resume_variant_id"], ["resume_variants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ledger", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ledger_client_key"), ["client_key"], unique=False)
        batch_op.create_index(batch_op.f("ix_ledger_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ledger_vendor_key"), ["vendor_key"], unique=False)

    op.create_table(
        "outbound_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("to_addr", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "attachments",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("in_reply_to", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("outbound_emails", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_outbound_emails_application_id"), ["application_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_outbound_emails_message_id"), ["message_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_outbound_emails_sent_at"), ["sent_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("outbound_emails", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_outbound_emails_sent_at"))
        batch_op.drop_index(batch_op.f("ix_outbound_emails_message_id"))
        batch_op.drop_index(batch_op.f("ix_outbound_emails_application_id"))

    op.drop_table("outbound_emails")
    with op.batch_alter_table("ledger", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ledger_vendor_key"))
        batch_op.drop_index(batch_op.f("ix_ledger_user_id"))
        batch_op.drop_index(batch_op.f("ix_ledger_client_key"))

    op.drop_table("ledger")
    with op.batch_alter_table("inbound_emails", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_inbound_emails_user_id"))

    op.drop_table("inbound_emails")
    with op.batch_alter_table("application_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_application_events_application_id"))

    op.drop_table("application_events")
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_applications_user_id"))
        batch_op.drop_index(batch_op.f("ix_applications_status"))
        batch_op.drop_index(batch_op.f("ix_applications_job_id"))

    op.drop_table("applications")
    with op.batch_alter_table("resume_variants", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_resume_variants_user_id"))
        batch_op.drop_index(batch_op.f("ix_resume_variants_job_id"))

    op.drop_table("resume_variants")
    with op.batch_alter_table("job_scores", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_scores_user_id"))
        batch_op.drop_index(batch_op.f("ix_job_scores_job_id"))
        batch_op.drop_index(batch_op.f("ix_job_scores_decision"))

    op.drop_table("job_scores")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_jobs_workplace"))
        batch_op.drop_index(batch_op.f("ix_jobs_source_id"))
        batch_op.drop_index("ix_jobs_first_seen")
        batch_op.drop_index(batch_op.f("ix_jobs_fingerprint"))
        batch_op.drop_index(batch_op.f("ix_jobs_company_key"))
        batch_op.drop_index("ix_jobs_closed")

    op.drop_table("jobs")
    with op.batch_alter_table("answers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_answers_user_id"))

    op.drop_table("answers")
    op.drop_table("users")
    op.drop_table("sources")
