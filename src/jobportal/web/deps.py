"""Shared pieces for the web routes: database session, config, templates, display helpers."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from jobportal.config import ConfigError, UserConfig, load_user_config
from jobportal.db import get_session_factory, utcnow
from jobportal.models import AppStatus, Job, User
from jobportal.settings import Settings, get_settings
from jobportal.users import get_default_user

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

STATUS_LABELS = {
    AppStatus.preparing.value: "Being prepared",
    AppStatus.needs_answers.value: "Needs your answer",
    AppStatus.needs_review.value: "Ready for your approval",
    AppStatus.approved.value: "Approved, sending",
    AppStatus.submitting.value: "Sending",
    AppStatus.needs_human.value: "Yours to finish",
    AppStatus.failed.value: "Did not go through",
    AppStatus.unconfirmed.value: "Not known whether it went out",
    AppStatus.skipped.value: "Not applying",
    AppStatus.submitted.value: "Sent",
    AppStatus.replied.value: "Reply received",
    AppStatus.interviewing.value: "Interviewing",
    AppStatus.offer.value: "Offer",
    AppStatus.rejected.value: "Closed",
    AppStatus.withdrawn.value: "Withdrawn",
}
EMPLOYMENT_LABELS = {
    "full_time": "Full-time",
    "contract": "Contract",
    "part_time": "Part-time",
    "internship": "Internship",
    "temporary": "Temporary",
}
WORKPLACE_LABELS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}


def ago(value: datetime | None, now: datetime | None = None) -> str:
    """'3h', '2d', 'just now' - short relative age."""
    if value is None:
        return ""
    seconds = max(0, int(((now or utcnow()) - value).total_seconds()))
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d" if days < 60 else f"{days // 30}mo"


def _zone(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def make_day_filter(settings: Settings) -> Any:
    zone = _zone(settings)

    def day(value: datetime | None, with_time: bool = False) -> str:
        if value is None:
            return ""
        local = value.astimezone(zone)
        text = f"{local.day} {local:%b %Y}"
        return f"{text}, {local:%H:%M}" if with_time else text

    return day


def pay(job: Job) -> str:
    """'$210-265k a year' / '$95-110 an hour', or '' when the posting does not say."""
    if job.comp_max is None and job.comp_min is None:
        return ""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$"}.get(
        job.comp_currency or "USD", ""
    )
    hourly = job.comp_period == "hour"

    def short(amount: float) -> str:
        if hourly or amount < 1000:
            return f"{amount:,.0f}"
        return f"{amount / 1000:.0f}k"

    low, high = job.comp_min, job.comp_max
    if low is not None and high is not None and low != high:
        amount = f"{symbol}{short(low)}-{short(high)}"
    else:
        amount = f"{symbol}{short(high if high is not None else low or 0)}"
    code = "" if symbol else f" {job.comp_currency}"
    return f"{amount}{code} {'an hour' if hourly else 'a year'}"


def fact_chips(job: Job) -> list[str]:
    """The short facts shown on a job card."""
    facts = job.facts or {}
    chips: list[str] = []
    if job.workplace:
        chips.append(WORKPLACE_LABELS.get(job.workplace, job.workplace))
    if job.employment_type:
        chips.append(EMPLOYMENT_LABELS.get(job.employment_type, job.employment_type))
    if facts.get("years_required"):
        chips.append(f"{facts['years_required']}+ years")
    if facts.get("clearance") == "required":
        chips.append(f"{facts.get('clearance_level') or 'Clearance'} required")
    elif facts.get("clearance") == "obtainable":
        chips.append("Clearance obtainable")
    if facts.get("sponsorship") == "not_offered":
        chips.append("No sponsorship")
    elif facts.get("sponsorship") == "offered":
        chips.append("Sponsorship offered")
    if facts.get("travel_percent") is not None:
        chips.append(f"{facts['travel_percent']}% travel")
    if facts.get("oncall"):
        chips.append("On-call")
    return chips


def build_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.trim_blocks = True
    env.lstrip_blocks = True
    env.filters["ago"] = ago
    env.filters["day"] = make_day_filter(settings)
    env.filters["pay"] = pay
    env.filters["status_label"] = lambda status: STATUS_LABELS.get(status, status)
    env.filters["employment"] = lambda value: EMPLOYMENT_LABELS.get(value, value or "")
    env.globals["fact_chips"] = fact_chips
    env.globals["workplace_labels"] = WORKPLACE_LABELS
    env.globals["employment_labels"] = EMPLOYMENT_LABELS
    return templates


# ------------------------------------------------------------- dependencies


def settings_dep() -> Settings:
    return get_settings()


def db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class ConfigProblem(Exception):
    """The YAML configuration is missing or invalid; rendered as a help page."""


def config_dep(settings: Settings = Depends(settings_dep)) -> UserConfig:
    try:
        return load_user_config(settings.data_dir)
    except ConfigError as exc:
        raise ConfigProblem(str(exc)) from exc


def user_dep(
    request: Request, session: Session = Depends(db), config: UserConfig = Depends(config_dep)
) -> User:
    user = get_default_user(session, config.profile)
    request.state.user_id = user.id  # for the page chrome (the queue badge)
    return user


# ------------------------------------------------------------------ helpers


def flash(request: Request, message: str, kind: str = "ok") -> None:
    # Reassigned rather than appended in place: the session only notices
    # changes made through its own keys.
    pending = list(request.session.get("flash", []))
    request.session["flash"] = [*pending[-4:], {"kind": kind, "message": message}]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = request.session.pop("flash", [])
    return messages


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def back(request: Request, default: str) -> Response:
    """After a form post: return to the page the form was on (same site only)."""
    target = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    if target and f"//{host}/" in target:
        path = "/" + target.split(f"//{host}/", 1)[1]
        return RedirectResponse(path, status_code=303)
    return RedirectResponse(default, status_code=303)


def render(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    base = {
        "flashes": pop_flashes(request) if not is_htmx(request) else [],
        "nav": request.app.state.nav_counts(request),
        "auth_enabled": request.app.state.settings.password is not None,
        "path": request.url.path,
    }
    return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)
