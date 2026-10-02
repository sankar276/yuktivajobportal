"""The submission ledger: who put your resume in front of which company.

Two vendors submitting you to the same client can get you disqualified, and
so can applying directly to a company a vendor already submitted you to. Every
application that goes out is recorded here, and every new one is checked
against it first.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.models import Application, Job, LedgerEntry, SourceKind
from jobportal.text import company_key

#: Marks a line written for a send whose outcome is not known. It guards
#: against a second route to the same client just like a confirmed line.
UNCONFIRMED_NOTE = "Unconfirmed: it is not known whether this arrived."


def is_vendor_role(job: Job) -> bool:
    """A requirement that reached you through a staffing vendor, not the employer."""
    raw = job.raw or {}
    if "vendor" in raw:
        return bool(raw["vendor"])
    return job.source.kind == SourceKind.email.value


def parties(job: Job) -> tuple[str, str]:
    """``(client, vendor)`` for a job. ``vendor`` is empty for a direct application."""
    if is_vendor_role(job):
        return job.client_name.strip(), job.company_name.strip()
    return job.company_name.strip(), ""


def find_conflict(
    session: Session,
    user_id: int,
    *,
    client: str,
    vendor: str,
    window_days: int,
    now: datetime,
    exclude_application_id: int | None = None,
) -> LedgerEntry | None:
    """An earlier submission to the same client through a *different* channel."""
    client_key = company_key(client)
    if not client_key:
        return None
    query = (
        select(LedgerEntry)
        .where(
            LedgerEntry.user_id == user_id,
            LedgerEntry.vendor_key != company_key(vendor),
            LedgerEntry.submitted_at >= now - timedelta(days=window_days),
        )
        .order_by(LedgerEntry.submitted_at.desc())
    )
    if exclude_application_id is not None:
        query = query.where(
            (LedgerEntry.application_id.is_(None))
            | (LedgerEntry.application_id != exclude_application_id)
        )
    # Compared in Python: vendors write the same client in different ways.
    for entry in session.scalars(query):
        if same_company(entry.client_key, client_key):
            return entry
    return None


def same_company(one: str, other: str) -> bool:
    """Do two company keys name the same company?

    Equal, or one is the start of the other once spaces are ignored
    ("jp morgan" and "jpmorgan chase"). This only ever raises a warning for
    you to judge, so it leans towards flagging.
    """
    if not one or not other:
        return False
    if one == other:
        return True
    a, b = one.replace(" ", ""), other.replace(" ", "")
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 5 and longer.startswith(shorter)


def describe(entry: LedgerEntry) -> str:
    via = f"through {entry.vendor_name}" if entry.vendor_name else "directly"
    role = f" for '{entry.role_title}'" if entry.role_title else ""
    if entry.notes == UNCONFIRMED_NOTE:
        return (
            f"An application to {entry.client_name} {via}{role} was started on "
            f"{entry.submitted_at:%d %b %Y} and it is not known whether it arrived."
        )
    return f"You were already submitted to {entry.client_name} {via} on {entry.submitted_at:%d %b %Y}{role}."


def record(
    session: Session,
    application: Application,
    *,
    now: datetime,
    engagement: str = "",
    rate: str = "",
    notes: str = "",
) -> LedgerEntry:
    """Write the ledger line for an application that has just gone out."""
    job = application.job
    client, vendor = parties(job)
    existing = session.scalar(
        select(LedgerEntry).where(LedgerEntry.application_id == application.id)
    )
    entry = existing or LedgerEntry(user_id=application.user_id, application_id=application.id)
    entry.client_name = client
    entry.client_key = company_key(client)
    entry.role_title = job.title
    entry.requisition_id = job.requisition_id or ""
    entry.vendor_name = vendor
    entry.vendor_key = company_key(vendor)
    entry.vendor_contact = job.contact_email or ""
    entry.channel = application.channel
    entry.engagement = engagement or (job.employment_type or "")
    entry.rate = rate
    entry.resume_variant_id = application.resume_variant_id
    entry.submitted_at = now
    if notes:
        entry.notes = notes
    elif entry.notes == UNCONFIRMED_NOTE:
        entry.notes = ""  # it is confirmed now
    session.add(entry)
    session.flush()
    return entry


def confirm(session: Session, application: Application) -> None:
    """An unconfirmed send turned out to have arrived: drop the "not known" mark."""
    for entry in session.scalars(
        select(LedgerEntry).where(
            LedgerEntry.application_id == application.id, LedgerEntry.notes == UNCONFIRMED_NOTE
        )
    ):
        entry.notes = ""


def forget_unconfirmed(session: Session, application: Application) -> None:
    """An unconfirmed send turned out *not* to have arrived: remove its line."""
    for entry in session.scalars(
        select(LedgerEntry).where(
            LedgerEntry.application_id == application.id, LedgerEntry.notes == UNCONFIRMED_NOTE
        )
    ):
        session.delete(entry)
