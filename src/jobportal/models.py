"""Database models.

Every table that belongs to a person carries ``user_id``. The first build runs
single-user (one row in ``users``), but nothing here assumes it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from jobportal.db import UTCDateTime, utcnow

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------- enums


class SourceKind(StrEnum):
    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    workday = "workday"
    email = "email"  # recruiter requirements read from your mailbox
    manual = "manual"  # roles you pasted in yourself


class SourceStatus(StrEnum):
    never = "never"
    ok = "ok"
    unchanged = "unchanged"
    not_found = "not_found"
    robots_blocked = "robots_blocked"
    error = "error"


class Decision(StrEnum):
    shortlist = "shortlist"
    consider = "consider"
    skip = "skip"


class AppStatus(StrEnum):
    # before sending
    needs_answers = "needs_answers"  # a required question has no answer yet
    needs_review = "needs_review"  # prepared; waiting for your one click
    approved = "approved"  # cleared to send (by you or by the auto policy)
    submitting = "submitting"
    needs_human = "needs_human"  # bot check, login wall or unsupported form
    failed = "failed"
    skipped = "skipped"  # blocked by policy or the ledger; see blockers
    # after sending
    submitted = "submitted"
    replied = "replied"
    interviewing = "interviewing"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"


#: Statuses that count as "this role has been applied to".
SENT_STATUSES = frozenset(
    {
        AppStatus.submitted,
        AppStatus.replied,
        AppStatus.interviewing,
        AppStatus.offer,
        AppStatus.rejected,
    }
)
#: Statuses that still need something from you before anything is sent.
WAITING_STATUSES = frozenset(
    {AppStatus.needs_answers, AppStatus.needs_review, AppStatus.needs_human, AppStatus.failed}
)
#: Statuses in which an application occupies its role (blocks a duplicate).
ACTIVE_STATUSES = frozenset(set(AppStatus) - {AppStatus.skipped, AppStatus.withdrawn})


# ------------------------------------------------------------------ tables


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Source(Base):
    """One place jobs come from: a company's board on an ATS, or your mailbox."""

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("kind", "token", name="uq_sources_kind_token"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    #: Identifier within the ATS (board token, site name, ``host/tenant/site``).
    token: Mapped[str] = mapped_column(String(255))
    company_name: Mapped[str] = mapped_column(String(200), default="")
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: False until the first successful crawl; jobs found then are a backlog, not news.
    initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    last_crawled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(32), default=SourceStatus.never.value)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    jobs_open: Mapped[int] = mapped_column(Integer, default=0)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    jobs: Mapped[list[Job]] = relationship(back_populates="source", cascade="all, delete-orphan")

    @property
    def label(self) -> str:
        return self.company_name or self.token


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_jobs_source_external"),
        Index("ix_jobs_first_seen", "first_seen_at"),
        Index("ix_jobs_closed", "closed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))

    company_name: Mapped[str] = mapped_column(String(200))
    company_key: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(500))
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)
    location: Mapped[str] = mapped_column(String(500), default="")
    remote: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    department: Mapped[str] = mapped_column(String(300), default="")
    requisition_id: Mapped[str] = mapped_column(String(120), default="")

    description_html: Mapped[str] = mapped_column(Text, default="")
    description_text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    apply_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: Found on the first crawl of its source, so its real age is unknown.
    is_backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Listed without a description; the detail page has not been fetched yet.
    needs_detail: Mapped[bool] = mapped_column(Boolean, default=False)

    comp_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: ``year`` or ``hour``.
    comp_period: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Vendor requirements (email / manual sources).
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    #: End client, when the vendor names one.
    client_name: Mapped[str] = mapped_column(String(200), default="")

    content_hash: Mapped[str] = mapped_column(String(64), default="")
    raw: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)

    source: Mapped[Source] = relationship(back_populates="jobs")
    scores: Mapped[list[JobScore]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    applications: Mapped[list[Application]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def effective_posted_at(self) -> datetime | None:
        """Best known publication time; ``None`` when the job was a backlog find."""
        if self.posted_at is not None:
            return self.posted_at
        return None if self.is_backfill else self.first_seen_at


class JobScore(Base):
    __tablename__ = "job_scores"
    __table_args__ = (UniqueConstraint("job_id", "user_id", name="uq_scores_job_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    lane: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    decision: Mapped[str] = mapped_column(String(16), default=Decision.skip.value, index=True)
    reasons: Mapped[list[str]] = mapped_column(JsonType, default=list)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    #: Hash of the rubric and job content this score was computed from.
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    scored_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    job: Mapped[Job] = relationship(back_populates="scores")


class ResumeVariant(Base):
    """A resume cut from the evidence bank for one job."""

    __tablename__ = "resume_variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    variant: Mapped[str] = mapped_column(String(64), default="default")
    pdf_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    docx_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    changes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    matched: Mapped[list[str]] = mapped_column(JsonType, default=list)
    gaps: Mapped[list[str]] = mapped_column(JsonType, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_applications_user_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), index=True)
    #: Sent by the auto policy, without a manual approval.
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    resume_variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_variants.id", ondelete="SET NULL"), nullable=True
    )
    #: What will be sent: the email draft, or the form fields and answers.
    prepared: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    #: Why it is not being sent yet: ``[{"kind": ..., "detail": ...}]``.
    blockers: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    follow_up_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_action: Mapped[str] = mapped_column(String(300), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    #: Proof of sending: message id, confirmation text or URL.
    confirmation: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    job: Mapped[Job] = relationship(back_populates="applications")
    resume_variant: Mapped[ResumeVariant | None] = relationship()
    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationEvent.id",
    )


class ApplicationEvent(Base):
    """Audit trail: everything that happened to an application, in order."""

    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(48))
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)

    application: Mapped[Application] = relationship(back_populates="events")


class LedgerEntry(Base):
    """Who represented you to which client, for what, and when.

    The guard against the same client receiving your resume from two vendors.
    """

    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True
    )
    client_name: Mapped[str] = mapped_column(String(200), default="")
    client_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    role_title: Mapped[str] = mapped_column(String(500), default="")
    requisition_id: Mapped[str] = mapped_column(String(120), default="")
    #: Empty when you applied to the company directly.
    vendor_name: Mapped[str] = mapped_column(String(200), default="")
    vendor_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    vendor_contact: Mapped[str] = mapped_column(String(320), default="")
    channel: Mapped[str] = mapped_column(String(16), default="")
    engagement: Mapped[str] = mapped_column(String(32), default="")
    rate: Mapped[str] = mapped_column(String(120), default="")
    resume_variant_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_variants.id", ondelete="SET NULL"), nullable=True
    )
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")


class Answer(Base):
    """The answer bank: a question you answered once is never asked again."""

    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("user_id", "question_key", name="uq_answers_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    question_key: Mapped[str] = mapped_column(String(300))
    question_text: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class OutboundEmail(Base):
    __tablename__ = "outbound_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    to_addr: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list[str]] = mapped_column(JsonType, default=list)
    message_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    in_reply_to: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)


class InboundEmail(Base):
    __tablename__ = "inbound_emails"
    __table_args__ = (UniqueConstraint("user_id", "message_id", name="uq_inbound_user_msg"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[str] = mapped_column(String(255))
    in_reply_to: Mapped[str] = mapped_column(String(255), default="")
    from_addr: Mapped[str] = mapped_column(String(320), default="")
    from_name: Mapped[str] = mapped_column(String(200), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, default="")
    #: ``requirement`` (a new role), ``reply`` (to something we sent) or ``other``.
    kind: Mapped[str] = mapped_column(String(16), default="other")
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
