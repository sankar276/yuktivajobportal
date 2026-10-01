"""The queue, the tracker, and everything you can do to an application."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from jobportal.apply import service
from jobportal.apply.service import ApplicationError
from jobportal.config import UserConfig
from jobportal.db import utcnow
from jobportal.models import SENT_STATUSES, Application, AppStatus, User
from jobportal.settings import Settings
from jobportal.web import queries
from jobportal.web.deps import back, config_dep, db, flash, render, settings_dep, user_dep

router = APIRouter()


def _application_or_404(session: Session, user: User, application_id: int) -> Application:
    application = session.get(Application, application_id)
    if application is None or application.user_id != user.id:
        raise HTTPException(status_code=404, detail="No such application.")
    return application


def _do(request: Request, action: object, success: str, application: Application) -> Response:
    """Run an action; show its result or the reason it is not allowed."""
    try:
        action()  # type: ignore[operator]
        flash(request, success)
    except ApplicationError as exc:
        flash(request, str(exc), "error")
    return back(request, f"/applications/{application.id}")


@router.get("/queue")
def queue_page(
    request: Request,
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    grouped = queries.queue(session, user.id)
    return render(
        request,
        "queue.html",
        {
            "grouped": grouped,
            "groups": queries.QUEUE_GROUPS,
            "in_flight": grouped[AppStatus.preparing.value]
            + grouped[AppStatus.approved.value]
            + grouped[AppStatus.submitting.value],
            "mode": config.search.policy.mode,
            "now": utcnow(),
        },
    )


@router.get("/tracker")
def tracker_page(
    request: Request,
    session: Session = Depends(db),
    _config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    grouped = queries.tracker(session, user.id)
    now = utcnow()
    due = [
        application
        for application in grouped[AppStatus.submitted.value]
        if application.follow_up_at is not None and application.follow_up_at <= now
    ]
    return render(
        request,
        "tracker.html",
        {"grouped": grouped, "stages": queries.TRACKER_STAGES, "due": due, "now": now},
    )


@router.get("/applications/{application_id}")
def application_page(
    request: Request,
    application_id: int,
    session: Session = Depends(db),
    _config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    return render(
        request,
        "application.html",
        {
            "application": application,
            "job": application.job,
            "prepared": application.prepared or {},
            "variant": application.resume_variant,
            "sent": application.status in {s.value for s in SENT_STATUSES},
            "stages": queries.TRACKER_STAGES,
            "now": utcnow(),
        },
    )


@router.post("/applications/{application_id}/approve")
def approve(
    request: Request,
    application_id: int,
    subject: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)

    def action() -> None:
        if application.channel == "email" and (subject.strip() or body.strip()):
            service.edit_draft(application, subject=subject, body=body)  # approve what is on screen
        service.approve(application)

    return _do(request, action, "Approved. It will go out within a minute.", application)


@router.post("/applications/{application_id}/draft")
def save_draft(
    request: Request,
    application_id: int,
    subject: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    return _do(
        request,
        lambda: service.edit_draft(application, subject=subject, body=body),
        "Draft saved.",
        application,
    )


@router.post("/applications/{application_id}/answers")
async def answers(
    request: Request,
    application_id: int,
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    form = await request.form()
    merged: dict[str, str] = {}
    for key, value in form.multi_items():
        text = str(value).strip()
        if key.startswith("answer:") and text:
            name = key.removeprefix("answer:")
            # Several ticked boxes for one question arrive as repeated fields.
            merged[name] = f"{merged[name]}; {text}" if name in merged else text
    try:
        saved = service.provide_answers(session, application, merged)
    except ApplicationError as exc:
        flash(request, str(exc), "error")
        return back(request, "/queue")
    if saved:
        flash(
            request, "Saved. Your answers are remembered for every later form that asks the same."
        )
    else:
        flash(request, "Nothing to save: fill in at least one answer.", "error")
    return back(request, "/queue")


@router.post("/applications/{application_id}/dismiss")
def dismiss(
    request: Request,
    application_id: int,
    reason: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    return _do(
        request, lambda: service.dismiss(application, reason.strip()), "Dismissed.", application
    )


@router.post("/applications/{application_id}/retry")
def retry(
    request: Request,
    application_id: int,
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)

    def action() -> None:
        allowed = {
            AppStatus.failed.value,
            AppStatus.needs_human.value,
            AppStatus.skipped.value,
            AppStatus.needs_answers.value,
        }
        if application.status not in allowed:
            raise ApplicationError(
                "This application cannot be prepared again from its current state."
            )
        application.status = AppStatus.preparing.value
        application.error = ""
        service.add_event(application, "requested_again")

    return _do(request, action, "Preparing it again.", application)


@router.post("/applications/{application_id}/mark-submitted")
def mark_submitted(
    request: Request,
    application_id: int,
    note: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    return _do(
        request,
        lambda: service.mark_submitted(session, config, application, note=note.strip()),
        "Recorded as sent and added to the ledger.",
        application,
    )


@router.post("/applications/{application_id}/stage")
def stage(
    request: Request,
    application_id: int,
    stage: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    return _do(
        request,
        lambda: service.set_stage(application, stage, note=note.strip()),
        "Updated.",
        application,
    )


@router.post("/applications/{application_id}/notes")
def notes(
    request: Request,
    application_id: int,
    notes: Annotated[str, Form()] = "",
    next_action: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    application.notes = notes.strip()[:5000]
    application.next_action = next_action.strip()[:300]
    flash(request, "Notes saved.")
    return back(request, f"/applications/{application.id}")


# ------------------------------------------------------------------- files


def _inside(path: Path, root: Path) -> Path:
    """Only serve files that really live under one of the app's own folders."""
    resolved = path.resolve()
    if not resolved.is_file() or not resolved.is_relative_to(root.resolve()):
        raise HTTPException(status_code=404, detail="File not found.")
    return resolved


@router.get("/applications/{application_id}/resume.{extension}")
def resume_file(
    application_id: int,
    extension: str,
    session: Session = Depends(db),
    settings: Settings = Depends(settings_dep),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    variant = application.resume_variant
    stored = (
        {"pdf": variant.pdf_path, "docx": variant.docx_path}.get(extension) if variant else None
    )
    if not stored:
        raise HTTPException(
            status_code=404, detail="No resume has been prepared for this application."
        )
    path = _inside(Path(stored), settings.resumes_dir)
    return FileResponse(path, filename=path.name)


@router.get("/applications/{application_id}/draft.eml")
def draft_file(
    application_id: int,
    session: Session = Depends(db),
    settings: Settings = Depends(settings_dep),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    stored = (application.prepared or {}).get("draft_file")
    if not stored:
        raise HTTPException(status_code=404, detail="No draft was saved for this application.")
    path = _inside(Path(stored), settings.outbox_dir)
    return FileResponse(path, filename=path.name, media_type="message/rfc822")


@router.get("/applications/{application_id}/screenshot/{index}")
def screenshot_file(
    application_id: int,
    index: int,
    session: Session = Depends(db),
    settings: Settings = Depends(settings_dep),
    user: User = Depends(user_dep),
) -> Response:
    application = _application_or_404(session, user, application_id)
    shots = (application.prepared or {}).get("screenshots") or []
    if not 0 <= index < len(shots):
        raise HTTPException(status_code=404, detail="No such screenshot.")
    return FileResponse(
        _inside(Path(shots[index]), settings.screenshots_dir), media_type="image/png"
    )
