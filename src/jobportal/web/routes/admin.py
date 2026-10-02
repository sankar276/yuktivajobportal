"""Ledger, sources, answer bank, settings and signing in."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.apply.answers import save_answer
from jobportal.config import UserConfig
from jobportal.crawl import add_source
from jobportal.db import utcnow
from jobportal.http import FetchError, PoliteClient
from jobportal.models import Answer, LedgerEntry, Source, User
from jobportal.settings import Settings
from jobportal.sources import ADAPTERS, discover_sources
from jobportal.text import company_key, squash
from jobportal.web.deps import back, config_dep, db, flash, render, settings_dep, user_dep
from jobportal.web.security import password_matches

router = APIRouter()


# ------------------------------------------------------------------- ledger


@router.get("/ledger")
def ledger_page(
    request: Request,
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    entries = session.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.user_id == user.id)
        .order_by(LedgerEntry.submitted_at.desc())
    ).all()
    return render(
        request,
        "ledger.html",
        {
            "entries": entries,
            "window_days": config.search.policy.ledger_window_days,
            "now": utcnow(),
        },
    )


@router.post("/ledger")
def ledger_add(
    request: Request,
    client_name: Annotated[str, Form()],
    role_title: Annotated[str, Form()] = "",
    vendor_name: Annotated[str, Form()] = "",
    rate: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    """Record a submission made outside the app, so it is guarded too."""
    client = squash(client_name)
    if not client:
        flash(request, "A client name is required.", "error")
        return back(request, "/ledger")
    vendor = squash(vendor_name)
    session.add(
        LedgerEntry(
            user_id=user.id,
            client_name=client[:200],
            client_key=company_key(client)[:200],
            role_title=squash(role_title)[:500],
            vendor_name=vendor[:200],
            vendor_key=company_key(vendor)[:200],
            channel="manual",
            rate=squash(rate)[:120],
            notes=notes.strip()[:2000],
            submitted_at=utcnow(),
        )
    )
    flash(request, f"Recorded your submission to {client}.")
    return back(request, "/ledger")


@router.post("/ledger/{entry_id}/delete")
def ledger_delete(
    request: Request, entry_id: int, session: Session = Depends(db), user: User = Depends(user_dep)
) -> Response:
    entry = session.get(LedgerEntry, entry_id)
    if entry is None or entry.user_id != user.id:
        raise HTTPException(status_code=404)
    session.delete(entry)
    flash(request, "Ledger line removed.")
    return back(request, "/ledger")


# ------------------------------------------------------------------ sources


@router.get("/sources")
def sources_page(
    request: Request, session: Session = Depends(db), _config: UserConfig = Depends(config_dep)
) -> Response:
    rows = session.scalars(select(Source).order_by(Source.company_name, Source.token)).all()
    boards = [s for s in rows if s.kind in ADAPTERS]
    return render(
        request,
        "sources.html",
        {
            "sources": boards,
            "adapters": ADAPTERS,
            "now": utcnow(),
            "worker": request.app.state.worker,
        },
    )


@router.post("/sources")
def sources_add(
    request: Request,
    url: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    session: Session = Depends(db),
    settings: Settings = Depends(settings_dep),
) -> Response:
    url = url.strip()
    if not url:
        flash(request, "Paste the address of a company's job board or careers page.", "error")
        return back(request, "/sources")
    try:
        with PoliteClient(settings) as client:
            specs = discover_sources(client, url)
    except FetchError as exc:
        flash(request, f"That page could not be read: {exc}", "error")
        return back(request, "/sources")
    if not specs:
        flash(
            request,
            "No supported job board found there (Greenhouse, Lever, Ashby, Workday). "
            "Open one of the company's job postings and paste that address instead.",
            "error",
        )
        return back(request, "/sources")
    added = 0
    for spec in specs:
        _source, created = add_source(session, spec, squash(name))
        added += created
    if added:
        flash(
            request,
            f"Now watching {added} board{'s' if added != 1 else ''}. It will be read on the next pass.",
        )
        worker = request.app.state.worker
        if worker is not None:
            worker.last_crawl = None  # read the new board on the next tick
    else:
        flash(request, "Already watching that board.")
    return back(request, "/sources")


@router.post("/sources/{source_id}/toggle")
def sources_toggle(request: Request, source_id: int, session: Session = Depends(db)) -> Response:
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    source.enabled = not source.enabled
    flash(request, f"{source.label}: {'watching again' if source.enabled else 'paused'}.")
    return back(request, "/sources")


@router.post("/sources/{source_id}/delete")
def sources_delete(request: Request, source_id: int, session: Session = Depends(db)) -> Response:
    source = session.get(Source, source_id)
    if source is None or source.kind not in ADAPTERS:
        raise HTTPException(status_code=404)
    label = source.label
    session.delete(source)
    flash(request, f"Stopped watching {label}; its postings were removed.")
    return back(request, "/sources")


@router.post("/sources/check-now")
def sources_check_now(request: Request) -> Response:
    worker = request.app.state.worker
    if worker is None:
        flash(
            request,
            "The background worker is not running in this process. Run `jobportal run`.",
            "error",
        )
    else:
        worker.request_crawl()
        flash(request, "Reading the boards now. New roles appear in the feed in a minute or two.")
    return back(request, "/sources")


# ------------------------------------------------------------------ answers


@router.get("/answers")
def answers_page(
    request: Request,
    session: Session = Depends(db),
    config: UserConfig = Depends(config_dep),
    user: User = Depends(user_dep),
) -> Response:
    rows = session.scalars(
        select(Answer).where(Answer.user_id == user.id).order_by(Answer.updated_at.desc())
    ).all()
    return render(request, "answers.html", {"answers": rows, "standard": config.profile.answers})


@router.post("/answers")
def answers_add(
    request: Request,
    question: Annotated[str, Form()],
    answer: Annotated[str, Form()],
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    if save_answer(session, user.id, question, answer) is None:
        flash(request, "Both a question and an answer are needed.", "error")
    else:
        flash(request, "Answer saved.")
    return back(request, "/answers")


@router.post("/answers/{answer_id}")
def answers_update(
    request: Request,
    answer_id: int,
    answer: Annotated[str, Form()],
    session: Session = Depends(db),
    user: User = Depends(user_dep),
) -> Response:
    row = session.get(Answer, answer_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404)
    if not answer.strip():
        flash(request, "An answer cannot be empty. Remove it instead.", "error")
    else:
        row.answer = answer.strip()
        flash(request, "Answer updated.")
    return back(request, "/answers")


@router.post("/answers/{answer_id}/delete")
def answers_delete(
    request: Request, answer_id: int, session: Session = Depends(db), user: User = Depends(user_dep)
) -> Response:
    row = session.get(Answer, answer_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404)
    session.delete(row)
    flash(request, "Answer removed. The next form that asks will wait for you.")
    return back(request, "/answers")


# ----------------------------------------------------------------- settings


@router.get("/settings")
def settings_page(
    request: Request,
    config: UserConfig = Depends(config_dep),
    settings: Settings = Depends(settings_dep),
) -> Response:
    worker = request.app.state.worker
    return render(
        request,
        "settings.html",
        {
            "config": config,
            "policy": config.search.policy,
            "profile": config.profile,
            "settings": settings,
            "worker": worker,
            "data_dir": settings.data_dir.resolve(),
        },
    )


# -------------------------------------------------------------------- login


@router.get("/login")
def login_page(request: Request, settings: Settings = Depends(settings_dep)) -> Response:
    if settings.password is None or request.session.get("authenticated"):
        return RedirectResponse("/feed", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/login")
def login(
    request: Request,
    password: Annotated[str, Form()] = "",
    settings: Settings = Depends(settings_dep),
) -> Response:
    address = request.client.host if request.client else "unknown"
    if not request.app.state.login_limiter.allow(address):
        return request.app.state.templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many attempts. Wait a minute and try again."},
            status_code=429,
        )
    if not password_matches(password, settings):
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {"error": "That is not the password."}, status_code=401
        )
    request.session.clear()
    request.session["authenticated"] = True
    return RedirectResponse("/feed", status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/healthz")
def healthz() -> Response:
    return PlainTextResponse("ok")
