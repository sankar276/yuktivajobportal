"""When may the app send an application without asking you first?

Only in ``auto`` mode, and only when every rule below holds. Anything else
waits in the queue for one click. The rules are checked when an application is
prepared and again at the moment of sending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from jobportal.config import Policy
from jobportal.models import (
    SENT_STATUSES,
    Application,
    AppStatus,
    Decision,
    Job,
    JobScore,
    OutboundEmail,
)

_SENT = [status.value for status in SENT_STATUSES]
_ABOUT_TO_SEND = [AppStatus.approved.value, AppStatus.submitting.value]


@dataclass
class AutoDecision:
    allowed: bool
    #: Why not, in plain words. Empty when allowed.
    reasons: list[str] = field(default_factory=list)


def _sent_or_queued(since: datetime) -> object:
    """Went out since ``since``, or is cleared to go out."""
    return or_(
        and_(Application.status.in_(_SENT), Application.submitted_at >= since),
        Application.status.in_(_ABOUT_TO_SEND),
    )


def _count(session: Session, query: Select[tuple[int]], exclude_id: int | None) -> int:
    if exclude_id is not None:
        query = query.where(Application.id != exclude_id)
    return session.scalar(query) or 0


def auto_decision(
    session: Session,
    user_id: int,
    policy: Policy,
    job: Job,
    score: JobScore | None,
    channel: str,
    *,
    now: datetime,
    exclude_application_id: int | None = None,
) -> AutoDecision:
    """May this application go out unattended? ``reasons`` lists every rule that says no.

    ``exclude_application_id`` keeps an application from counting against its
    own caps when the rules are re-checked just before sending.
    """
    auto = policy.auto
    if policy.mode != "auto":
        return AutoDecision(False, ["Review mode: every application waits for your approval"])

    reasons: list[str] = []
    if channel not in {str(c) for c in auto.channels}:
        reasons.append(f"Unattended sending is not enabled for {channel} applications")
    if score is None or score.decision != Decision.shortlist.value:
        reasons.append("Not on the shortlist")
    elif score.score < auto.min_score:
        reasons.append(
            f"Scores {score.score:.0f}, below your unattended threshold of {auto.min_score:.0f}"
        )

    posted = job.effective_posted_at
    if posted is None:
        reasons.append("The posting's age is unknown")
    elif now - posted > timedelta(days=policy.max_job_age_days):
        reasons.append(f"Posted more than {policy.max_job_age_days} days ago")

    unattended = _count(
        session,
        select(func.count())
        .select_from(Application)
        .where(
            Application.user_id == user_id,
            Application.auto.is_(True),
            _sent_or_queued(now - timedelta(days=1)),
        ),
        exclude_application_id,
    )
    if unattended >= auto.daily_cap:
        reasons.append(f"Daily cap of {auto.daily_cap} unattended applications reached")

    to_company = _count(
        session,
        select(func.count())
        .select_from(Application)
        .join(Job, Job.id == Application.job_id)
        .where(
            Application.user_id == user_id,
            Job.company_key == job.company_key,
            _sent_or_queued(now - timedelta(days=7)),
        ),
        exclude_application_id,
    )
    if to_company >= auto.per_company_per_week:
        reasons.append(
            f"Already {to_company} applications to {job.company_name} this week "
            f"(limit {auto.per_company_per_week})"
        )
    return AutoDecision(not reasons, reasons)


def email_send_allowed(session: Session, policy: Policy, *, now: datetime) -> tuple[bool, str]:
    """Throttle for outgoing mail: a daily cap and a minimum gap between sends."""
    rules = policy.email
    sent_today = (
        session.scalar(
            select(func.count())
            .select_from(OutboundEmail)
            .where(OutboundEmail.status == "sent", OutboundEmail.sent_at >= now - timedelta(days=1))
        )
        or 0
    )
    if sent_today >= rules.daily_cap:
        return False, f"Daily cap of {rules.daily_cap} emails reached"
    last = session.scalar(
        select(func.max(OutboundEmail.sent_at)).where(OutboundEmail.status == "sent")
    )
    if last is not None and rules.min_seconds_between_sends:
        wait = rules.min_seconds_between_sends - (now - last).total_seconds()
        if wait > 0:
            return False, f"Next email can go out in {int(wait) + 1}s"
    return True, ""
