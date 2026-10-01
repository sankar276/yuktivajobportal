"""Read queries behind the pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from jobportal.models import (
    SENT_STATUSES,
    WAITING_STATUSES,
    Application,
    AppStatus,
    Decision,
    Job,
    JobScore,
)

PAGE_SIZE = 40
VIEWS = ("fresh", "shortlist", "all", "saved", "hidden")

#: Best known publication time; NULL when the job was a backlog find.
EFFECTIVE_POSTED = case(
    (Job.posted_at.is_not(None), Job.posted_at),
    (Job.is_backfill.is_(False), Job.first_seen_at),
    else_=None,
)


@dataclass
class FeedFilters:
    view: str = "shortlist"
    lane: str = ""
    workplace: str = ""
    commitment: str = ""
    posted: int | None = None  # days
    min_pay: int | None = None
    q: str = ""
    sort: str = "score"
    page: int = 1

    def params(self, **changes: Any) -> dict[str, Any]:
        """Query-string parameters for a link that keeps (or changes) these filters."""
        values = {
            "view": self.view,
            "lane": self.lane,
            "workplace": self.workplace,
            "commitment": self.commitment,
            "posted": self.posted,
            "min_pay": self.min_pay,
            "q": self.q,
            "sort": self.sort if self.sort != "score" else "",
            "page": self.page if self.page > 1 else "",
        }
        values.update(changes)
        return {key: value for key, value in values.items() if value not in ("", None)}

    @property
    def narrowed(self) -> bool:
        return bool(
            self.lane or self.workplace or self.commitment or self.posted or self.min_pay or self.q
        )


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def _base(user_id: int) -> Select[tuple[Job, JobScore, Application]]:
    return (
        select(Job, JobScore, Application)
        .join(JobScore, and_(JobScore.job_id == Job.id, JobScore.user_id == user_id))
        .outerjoin(Application, and_(Application.job_id == Job.id, Application.user_id == user_id))
        .where(Job.closed_at.is_(None))
    )


def _apply_view(query: Any, view: str, *, fresh_since: datetime) -> Any:
    if view == "hidden":
        return query.where(JobScore.hidden.is_(True))
    query = query.where(JobScore.hidden.is_(False))
    if view == "saved":
        return query.where(JobScore.saved.is_(True))
    if view == "fresh":
        return query.where(
            JobScore.decision != Decision.skip.value, fresh_since <= EFFECTIVE_POSTED
        )
    if view == "shortlist":
        return query.where(JobScore.decision == Decision.shortlist.value)
    return query


def _apply_filters(query: Any, filters: FeedFilters, now: datetime) -> Any:
    if filters.lane:
        query = query.where(JobScore.lane == filters.lane)
    if filters.workplace:
        query = query.where(Job.workplace == filters.workplace)
    if filters.commitment:
        query = query.where(Job.employment_type == filters.commitment)
    if filters.posted:
        query = query.where(now - timedelta(days=filters.posted) <= EFFECTIVE_POSTED)
    if filters.min_pay:
        period = "hour" if filters.min_pay < 1000 else "year"
        query = query.where(Job.comp_period == period, Job.comp_max >= filters.min_pay)
    if filters.q.strip():
        pattern = _like(filters.q.strip())
        query = query.where(
            or_(Job.title.ilike(pattern, escape="\\"), Job.company_name.ilike(pattern, escape="\\"))
        )
    return query


def feed(
    session: Session, user_id: int, filters: FeedFilters, *, fresh_hours: int, now: datetime
) -> tuple[list[tuple[Job, JobScore, Application | None]], int]:
    """One page of the feed and the total number of matching roles."""
    fresh_since = now - timedelta(hours=fresh_hours)
    query = _apply_filters(
        _apply_view(_base(user_id), filters.view, fresh_since=fresh_since), filters, now
    )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    newest = EFFECTIVE_POSTED.desc().nulls_last()
    order = (
        (newest, JobScore.score.desc())
        if filters.sort == "newest"
        else (JobScore.score.desc(), newest)
    )
    rows = session.execute(
        query.order_by(*order, Job.id.desc())
        .limit(PAGE_SIZE)
        .offset((max(filters.page, 1) - 1) * PAGE_SIZE)
    ).all()
    return [(job, score, application) for job, score, application in rows], total


def view_counts(
    session: Session, user_id: int, *, fresh_hours: int, now: datetime
) -> dict[str, int]:
    fresh_since = now - timedelta(hours=fresh_hours)
    counts = {}
    for view in ("fresh", "shortlist", "all", "saved"):
        query = _apply_view(_base(user_id), view, fresh_since=fresh_since)
        counts[view] = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    return counts


def waiting_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Application)
            .where(Application.status.in_([s.value for s in WAITING_STATUSES]))
        )
        or 0
    )


QUEUE_GROUPS: list[tuple[str, str, str]] = [
    (AppStatus.needs_answers.value, "Needs your answer", "A form asked something you have not answered before. Answer once and it is remembered."),
    (AppStatus.needs_review.value, "Ready for your approval", "Prepared and waiting. Nothing is sent until you approve."),
    (AppStatus.needs_human.value, "Yours to finish", "The site wants a person: a bot check, a login, or a form the app cannot operate."),
    (AppStatus.failed.value, "Did not go through", "Check what happened before trying again."),
]  # fmt: skip


def queue(session: Session, user_id: int) -> dict[str, list[Application]]:
    statuses = [group[0] for group in QUEUE_GROUPS] + [
        AppStatus.preparing.value,
        AppStatus.approved.value,
        AppStatus.submitting.value,
    ]
    rows = session.scalars(
        select(Application)
        .where(Application.user_id == user_id, Application.status.in_(statuses))
        .order_by(Application.updated_at.desc(), Application.id.desc())
    ).all()
    grouped: dict[str, list[Application]] = {status: [] for status in statuses}
    for application in rows:
        grouped[application.status].append(application)
    return grouped


TRACKER_STAGES = [
    AppStatus.submitted.value,
    AppStatus.replied.value,
    AppStatus.interviewing.value,
    AppStatus.offer.value,
    AppStatus.rejected.value,
    AppStatus.withdrawn.value,
]


def tracker(session: Session, user_id: int) -> dict[str, list[Application]]:
    rows = session.scalars(
        select(Application)
        .where(
            Application.user_id == user_id,
            Application.status.in_([s.value for s in SENT_STATUSES] + [AppStatus.withdrawn.value]),
        )
        .order_by(Application.submitted_at.desc().nulls_last(), Application.id.desc())
    ).all()
    grouped: dict[str, list[Application]] = {stage: [] for stage in TRACKER_STAGES}
    for application in rows:
        grouped.setdefault(application.status, []).append(application)
    return grouped
