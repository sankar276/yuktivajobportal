"""The feed, a single job, and what you can do to a job."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.apply import service
from jobportal.apply.service import ApplicationError
from jobportal.config import UserConfig
from jobportal.db import utcnow
from jobportal.manual import ManualJobError, add_manual_job
from jobportal.models import Application, AppStatus, Job, JobScore, ResumeVariant, User
from jobportal.scoring import score_jobs
from jobportal.web import queries
from jobportal.web.deps import back, config_dep, db, flash, is_htmx, render, user_dep

router = APIRouter()


def _int(value: str) -> int | None:
    try:
        return int(value) if value.strip() else None
    except ValueError:
        return None


def _job_or_404(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return job


def _score_row(session: Session, user: User, job: Job, config: UserConfig) -> JobScore:
    row = session.scalar(
        select(JobScore).where(JobScore.job_id == job.id, JobScore.user_id == user.id)
    )
    if row is None:  # added a moment ago and not scored yet
        score_jobs(session, user.id, config.search, job_ids=[job.id], profile=config.profile)
        row = session.scalar(
            select(JobScore).where(JobScore.job_id == job.id, JobScore.user_id == user.id)
        )
    assert row is not None
    return row


def _application(session: Session, user: User, job: Job) -> Application | None:
    return session.scalar(
        select(Application).where(Application.job_id == job.id, Application.user_id == user.id)
    )


@router.get("/")
def home() -> Response:
    return RedirectResponse("/feed", status_code=303)


@router.get("/feed")
def feed_page(
    request: Request,
    view: str = "",
    lane: str = "",
    workplace: str = "",
    commitment: str = "",
    posted: str = "",
    min_pay: str = "",
    q: str = "",
    sort: str = "score",
    page: str = "1",
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    now = utcnow()
    fresh_hours = config.search.policy.fresh_hours
    counts = queries.view_counts(session, user.id, fresh_hours=fresh_hours, now=now)
    if view not in queries.VIEWS:
        # Land on what is new; fall back to the shortlist when nothing is.
        view = "fresh" if counts["fresh"] else "shortlist"
    filters = queries.FeedFilters(
        view=view,
        lane=lane if config.search.lane(lane) else "",
        workplace=workplace if workplace in ("remote", "hybrid", "onsite") else "",
        commitment=commitment,
        posted=_int(posted),
        min_pay=_int(min_pay),
        currency=(config.search.lane(lane) or config.search.lanes[0]).compensation.currency,
        q=q.strip()[:100],
        sort="newest" if sort == "newest" else "score",
        page=max(_int(page) or 1, 1),
    )
    rows, total = queries.feed(session, user.id, filters, fresh_hours=fresh_hours, now=now)
    pages = max(1, -(-total // queries.PAGE_SIZE))
    return render(
        request,
        "feed.html",
        {
            "rows": rows,
            "total": total,
            "filters": filters,
            "counts": counts,
            "pages": pages,
            "lanes": config.search.lanes,
            "fresh_hours": fresh_hours,
            "now": now,
        },
    )


@router.get("/jobs/new")
def new_job_page(request: Request, _config: UserConfig = Depends(config_dep)) -> Response:
    return render(request, "job_new.html", {"values": {}, "error": ""})


@router.post("/jobs")
def create_job(
    request: Request,
    title: Annotated[str, Form()] = "",
    company: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    location: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    employment_type: Annotated[str, Form()] = "",
    contact_name: Annotated[str, Form()] = "",
    contact_email: Annotated[str, Form()] = "",
    client_name: Annotated[str, Form()] = "",
    via_vendor: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    values = {
        "title": title, "company": company, "url": url, "location": location,
        "description": description, "employment_type": employment_type,
        "contact_name": contact_name, "contact_email": contact_email,
        "client_name": client_name, "via_vendor": via_vendor,
    }  # fmt: skip
    try:
        job = add_manual_job(
            session,
            title=title,
            company=company,
            url=url,
            location=location,
            description=description,
            employment_type=employment_type or None,
            contact_name=contact_name,
            contact_email=contact_email,
            client_name=client_name,
            via_vendor=bool(via_vendor),
        )
    except ManualJobError as exc:
        return render(
            request, "job_new.html", {"values": values, "error": str(exc)}, status_code=400
        )
    score_jobs(session, user.id, config.search, job_ids=[job.id], profile=config.profile)
    flash(request, f"Added {job.title} at {job.company_name}.")
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/jobs/{job_id}")
def job_page(
    request: Request,
    job_id: int,
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    job = _job_or_404(session, job_id)
    score = _score_row(session, user, job, config)
    application = _application(session, user, job)
    variant = session.scalar(
        select(ResumeVariant)
        .where(ResumeVariant.job_id == job.id, ResumeVariant.user_id == user.id)
        .order_by(ResumeVariant.id.desc())
    )
    lane = config.search.lane(score.lane)
    return render(
        request,
        "job.html",
        {
            "job": job,
            "score": score,
            "application": application,
            "variant": variant,
            "lane": lane,
            "now": utcnow(),
        },
    )


def _after_action(
    request: Request, session: Session, config: UserConfig, user: User, job: Job, message: str
) -> Response:
    """Re-render just the row for in-page updates; otherwise go back with a note."""
    if is_htmx(request) and request.headers.get("hx-target", "").startswith("job-"):
        session.flush()
        return render(
            request,
            "_job_row.html",
            {
                "job": job,
                "score": _score_row(session, user, job, config),
                "application": _application(session, user, job),
                "now": utcnow(),
                "note": message,
            },
        )
    flash(request, message)
    return back(request, f"/jobs/{job.id}")


@router.post("/jobs/{job_id}/save")
def toggle_saved(
    request: Request,
    job_id: int,
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    job = _job_or_404(session, job_id)
    score = _score_row(session, user, job, config)
    score.saved = not score.saved
    return _after_action(
        request, session, config, user, job, "Saved." if score.saved else "Removed from saved."
    )


@router.post("/jobs/{job_id}/hide")
def toggle_hidden(
    request: Request,
    job_id: int,
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    job = _job_or_404(session, job_id)
    score = _score_row(session, user, job, config)
    score.hidden = not score.hidden
    return _after_action(
        request, session, config, user, job, "Hidden." if score.hidden else "Back in the feed."
    )


@router.post("/jobs/{job_id}/apply")
def apply_to_job(
    request: Request,
    job_id: int,
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    job = _job_or_404(session, job_id)
    application = service.request_application(session, user, job)
    if application.status == AppStatus.preparing.value:
        message = "Preparing: tailoring your resume and reading the application. It will appear in the queue."
    else:
        message = "This role already has an application."
    return _after_action(request, session, config, user, job, message)


@router.post("/jobs/{job_id}/applied")
def mark_applied(
    request: Request,
    job_id: int,
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    """You applied somewhere else (the company site, a referral): record it."""
    job = _job_or_404(session, job_id)
    application = _application(session, user, job)
    if application is None:
        application = Application(
            user_id=user.id, job_id=job.id, channel="manual", status=AppStatus.needs_human.value
        )
        session.add(application)
        session.flush()
    try:
        service.mark_submitted(session, config, application, note="Applied outside the app.")
        message = "Recorded as applied."
    except ApplicationError as exc:
        message = str(exc)
    return _after_action(request, session, config, user, job, message)
