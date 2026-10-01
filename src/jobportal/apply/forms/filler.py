"""Fill and submit an application form in a real browser.

Three modes, in increasing order of trust:

``prepare``  Open the page, read the form, decide what would go where. Nothing
             is typed and nothing is sent.
``assist``   A visible browser: everything known is filled in, and *you*
             review, complete and submit. For forms behind a bot check or
             with anything the filler cannot handle.
``submit``   Unattended: fill and submit. Refused unless the form has no bot
             check, no login wall, no unanswered required question and no
             control the filler cannot operate.

The browser is not disguised and bot checks are never worked around: a form
that runs one is yours to submit. A submission only counts as sent when the
site confirms it.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from jobportal.apply.answers import AnswerBook
from jobportal.apply.forms.fields import FieldKind, FormField, Resolution, to_dict
from jobportal.http import PoliteClient
from jobportal.settings import Settings

log = logging.getLogger(__name__)

SCAN_JS = (Path(__file__).parent / "scan.js").read_text(encoding="utf-8")
NAVIGATION_TIMEOUT_MS = 30_000
OUTCOME_TIMEOUT_S = 25.0
_CONFIRM_RE = re.compile(
    r"thank you|thanks for (?:applying|your (?:application|interest))"
    r"|application (?:has been|was|is) (?:received|submitted|sent)"
    r"|we(?:'ve| have) received your application|successfully (?:submitted|applied)"
    r"|application (?:submitted|received|complete)|your application is on its way",
    re.IGNORECASE,
)
_CONFIRM_URL_RE = re.compile(r"confirm|thank|success|submitted|complete", re.IGNORECASE)
_ERROR_SELECTOR = (
    "[aria-invalid='true'], .error, .field-error, .error-message, [role='alert'], .invalid-feedback"
)
_HIGHLIGHT_CSS = "outline: 3px solid #d97706 !important; outline-offset: 2px !important;"


class FormUrlRefused(ValueError):
    """The application URL is not something the filler will open."""


@dataclass
class PlannedField:
    field: FormField
    kind: FieldKind
    resolution: Resolution


@dataclass
class FormPlan:
    url: str
    title: str = ""
    fill: list[PlannedField] = field(default_factory=list)
    #: Required, and you have not provided an answer.
    unanswered: list[FormField] = field(default_factory=list)
    #: Optional and unanswered: left blank.
    left_blank: list[FormField] = field(default_factory=list)
    #: Reasons the form cannot be submitted unattended.
    blockers: list[dict[str, str]] = field(default_factory=list)
    submit_ref: str | None = None
    submit_text: str = ""

    @property
    def status(self) -> str:
        if self.blockers:
            return "needs_human"
        return "needs_answers" if self.unanswered else "ready"

    def to_prepared(self) -> dict[str, Any]:
        """The record kept on the application and shown in the queue."""
        return {
            "channel": "form",
            "url": self.url,
            "page_title": self.title,
            "fields": [
                {
                    **to_dict(planned.field),
                    "value": Path(planned.resolution.value).name
                    if planned.field.type == "file"
                    else planned.resolution.value,
                    "source": planned.resolution.source,
                }
                for planned in self.fill
            ],
            "unanswered": [to_dict(form_field) for form_field in self.unanswered],
            "left_blank": [form_field.label or form_field.name for form_field in self.left_blank],
            "submit_text": self.submit_text,
        }


@dataclass
class FormOutcome:
    #: ready | needs_answers | needs_human | submitted | failed
    status: str
    plan: FormPlan | None = None
    confirmation: str = ""
    error: str = ""
    screenshots: list[str] = field(default_factory=list)
    blockers: list[dict[str, str]] = field(default_factory=list)


# -------------------------------------------------------------------- guard


def check_form_url(url: str, settings: Settings) -> None:
    """Only public https pages; local addresses only when explicitly allowed."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        raise FormUrlRefused(f"not a URL: {url!r}")
    local = host in ("localhost",) or host.endswith(".localhost")
    try:
        address = ipaddress.ip_address(host)
        local = local or address.is_private or address.is_loopback or address.is_link_local
    except ValueError:
        pass
    if local:
        if not settings.allow_local_forms:
            raise FormUrlRefused(f"refusing to open a local address: {host}")
        return
    if parts.scheme != "https":
        raise FormUrlRefused(f"refusing to open a non-https application page: {url}")


# --------------------------------------------------------------------- scan


def _open(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    # Pages with long-polling never go idle; the form is usually there anyway.
    with suppress(PlaywrightTimeout):
        page.wait_for_load_state("networkidle", timeout=8_000)
    with suppress(PlaywrightTimeout):
        page.wait_for_selector("input, textarea, select", timeout=5_000, state="attached")


def scan(page: Page) -> dict[str, Any]:
    result: dict[str, Any] = page.evaluate(SCAN_JS)
    return result


def build_plan(scanned: dict[str, Any], book: AnswerBook) -> FormPlan:
    plan = FormPlan(
        url=scanned.get("url", ""),
        title=scanned.get("title", ""),
        submit_ref=scanned.get("submit"),
        submit_text=scanned.get("submitText", ""),
    )
    if scanned.get("interstitial"):
        plan.blockers.append(
            {"kind": "bot_check", "detail": "The site showed a browser check instead of the form."}
        )
    if scanned.get("login"):
        plan.blockers.append(
            {
                "kind": "login",
                "detail": "The site asks you to sign in or create an account to apply.",
            }
        )
    if scanned.get("captcha"):
        plan.blockers.append(
            {
                "kind": "bot_check",
                "detail": f"The form is protected by a bot check ({scanned['captcha']}), so it is yours to submit.",
            }
        )

    fields = [
        FormField(
            ref=item["ref"],
            label=item.get("label", ""),
            type=item.get("type", "text"),
            required=bool(item.get("required")),
            name=item.get("name", ""),
            options=list(item.get("options") or []),
            prefilled=bool(item.get("prefilled")),
        )
        for item in scanned.get("fields", [])
    ]
    if not fields:
        plan.blockers.append(
            {"kind": "no_form", "detail": "No application form was found on the page."}
        )
        return plan

    for form_field in fields:
        resolution = book.resolve(form_field)
        if resolution is not None:
            plan.fill.append(PlannedField(form_field, book.kind_of(form_field), resolution))
        elif form_field.required and not form_field.prefilled:
            if form_field.type == "file":
                plan.blockers.append(
                    {
                        "kind": "unsupported",
                        "detail": f"The form requires an upload this app does not produce: {form_field.label or form_field.name}",
                    }
                )
            else:
                plan.unanswered.append(form_field)
        else:
            plan.left_blank.append(form_field)

    if not plan.submit_ref:
        plan.blockers.append(
            {
                "kind": "no_submit",
                "detail": "No submit button was found (the form may have several steps).",
            }
        )
    return plan


# --------------------------------------------------------------------- fill


def _apply(page: Page, planned: PlannedField, book: AnswerBook) -> None:
    form_field, resolution = planned.field, planned.resolution
    locator = page.locator(form_field.ref)
    if form_field.type == "file":
        locator.set_input_files(resolution.value)
    elif form_field.type == "select":
        assert resolution.option is not None
        locator.select_option(value=resolution.option["ref"])
    elif form_field.type in ("radio", "checkbox"):
        for option in book.options_for(form_field, resolution):
            _tick(page, option["ref"])
    elif form_field.type == "combobox":
        _choose_combobox(page, form_field, resolution.value)
    else:
        locator.fill(resolution.value)


def _tick(page: Page, ref: str) -> None:
    locator = page.locator(ref)
    try:
        locator.check(timeout=2_000)
    except PlaywrightError:
        # Custom-styled inputs are visually hidden; clicking the node still
        # runs the page's own handlers.
        locator.evaluate("el => { if (!el.checked) el.click(); }")


def _choose_combobox(page: Page, form_field: FormField, value: str) -> None:
    locator = page.locator(form_field.ref)
    locator.click()
    locator.fill(value)
    option = page.get_by_role(
        "option", name=re.compile(rf"^\s*{re.escape(value)}\s*$", re.IGNORECASE)
    ).first
    try:
        option.click(timeout=4_000)
    except PlaywrightError as exc:
        raise PlaywrightError(f"no option {value!r} offered for {form_field.label!r}") from exc


def _fill_all(page: Page, plan: FormPlan, book: AnswerBook) -> list[str]:
    """Fill everything planned. Returns a list of fields that could not be filled."""
    failed: list[str] = []
    for planned in plan.fill:
        try:
            _apply(page, planned, book)
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0]
            failed.append(f"{planned.field.label or planned.field.name}: {message}")
    return failed


def _screenshot(page: Page, directory: Path | None, name: str) -> str | None:
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    try:
        page.screenshot(path=str(path), full_page=True)
    except PlaywrightError as exc:
        log.warning("screenshot failed: %s", exc)
        return None
    return str(path)


def _visible_errors(page: Page) -> list[str]:
    try:
        texts = page.locator(_ERROR_SELECTOR).evaluate_all(
            "els => els.filter(e => e.offsetParent !== null).map(e => (e.innerText || '').trim()).filter(Boolean)"
        )
    except PlaywrightError:
        return []
    return list(dict.fromkeys(text[:200] for text in texts))[:5]


def _confirm_phrases(body: str) -> set[str]:
    return {match.group(0).lower() for match in _CONFIRM_RE.finditer(body)}


def _body(page: Page) -> str:
    try:
        return page.inner_text("body", timeout=2_000)
    except PlaywrightError:
        return ""


def _await_outcome(
    page: Page,
    before_url: str,
    timeout_s: float,
    *,
    baseline: set[str],
    fail_on_errors: bool,
) -> tuple[str, str]:
    """``("submitted", evidence)``, ``("failed", why)`` or ``("unknown", "")``.

    ``baseline`` holds confirmation-like phrases already on the page before it
    was submitted ("Thank you for your interest in Acme" in a form's intro),
    so they are not mistaken for a confirmation.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if page.is_closed():
            return "unknown", ""
        try:
            body = page.inner_text("body", timeout=2_000)
            url = page.url
            controls = page.locator("input:not([type='hidden']), textarea, select").count()
        except PlaywrightError:
            time.sleep(0.3)  # mid-navigation
            continue
        phrases = _confirm_phrases(body)
        fresh = phrases - baseline
        if fresh or (phrases and controls == 0):
            match = next(
                m for m in _CONFIRM_RE.finditer(body) if not fresh or m.group(0).lower() in fresh
            )
            start = max(0, match.start() - 60)
            return "submitted", " ".join(body[start : match.end() + 160].split())
        moved = urlsplit(url).path != urlsplit(before_url).path
        if moved and _CONFIRM_URL_RE.search(urlsplit(url).path):
            return "submitted", f"Redirected to {url}"
        if fail_on_errors and not moved:
            errors = _visible_errors(page)
            if errors:
                return "failed", "The form reported: " + "; ".join(errors)
        time.sleep(0.4)
    return "unknown", ""


# -------------------------------------------------------------------- modes


def _robots_blocker(url: str, client: PoliteClient | None) -> dict[str, str] | None:
    if client is None:
        return None
    try:
        allowed = client.allowed(url)
    except Exception as exc:  # robots lookup must never crash a run
        log.warning("robots check failed for %s: %s", url, exc)
        allowed = False
    if allowed:
        return None
    return {
        "kind": "robots",
        "detail": "The site's robots.txt asks automated clients not to open this page, so it is yours to open.",
    }


def prepare(
    browser: Browser,
    url: str,
    book: AnswerBook,
    *,
    settings: Settings,
    client: PoliteClient | None = None,
) -> FormOutcome:
    """Read the form and plan the answers. Types nothing, sends nothing."""
    try:
        check_form_url(url, settings)
    except FormUrlRefused as exc:
        return FormOutcome(status="needs_human", blockers=[{"kind": "url", "detail": str(exc)}])
    blocked = _robots_blocker(url, client)
    if blocked:
        return FormOutcome(status="needs_human", blockers=[blocked])

    page = browser.new_page()
    try:
        _open(page, url)
        plan = build_plan(scan(page), book)
    except PlaywrightError as exc:
        message = str(exc).splitlines()[0]
        return FormOutcome(
            status="needs_human",
            blockers=[
                {"kind": "page_error", "detail": f"The application page did not load: {message}"}
            ],
        )
    finally:
        page.close()
    return FormOutcome(status=plan.status, plan=plan, blockers=list(plan.blockers))


def submit(
    browser: Browser,
    url: str,
    book: AnswerBook,
    *,
    settings: Settings,
    client: PoliteClient | None = None,
    screenshot_dir: Path | None = None,
    label: str = "application",
) -> FormOutcome:
    """Fill and submit unattended. Refuses unless the form is fully automatable."""
    try:
        check_form_url(url, settings)
    except FormUrlRefused as exc:
        return FormOutcome(status="needs_human", blockers=[{"kind": "url", "detail": str(exc)}])
    blocked = _robots_blocker(url, client)
    if blocked:
        return FormOutcome(status="needs_human", blockers=[blocked])

    page = browser.new_page()
    shots: list[str] = []
    try:
        _open(page, url)
        plan = build_plan(scan(page), book)
        if plan.status != "ready":
            return FormOutcome(status=plan.status, plan=plan, blockers=list(plan.blockers))

        failed = _fill_all(page, plan, book)
        if failed:
            return FormOutcome(
                status="needs_human",
                plan=plan,
                blockers=[
                    {"kind": "unsupported", "detail": "Could not fill: " + "; ".join(failed)}
                ],
            )
        # A bot check can appear only once the form is touched.
        after = scan(page)
        if after.get("captcha") or after.get("login"):
            detail = "A bot check appeared while filling the form, so it is yours to submit."
            return FormOutcome(
                status="needs_human", plan=plan, blockers=[{"kind": "bot_check", "detail": detail}]
            )

        shot = _screenshot(page, screenshot_dir, f"{label}-filled.png")
        if shot:
            shots.append(shot)
        before_url = page.url
        baseline = _confirm_phrases(_body(page))
        assert plan.submit_ref is not None
        page.locator(plan.submit_ref).click()
        state, evidence = _await_outcome(
            page, before_url, OUTCOME_TIMEOUT_S, baseline=baseline, fail_on_errors=True
        )
        shot = _screenshot(page, screenshot_dir, f"{label}-after-submit.png")
        if shot:
            shots.append(shot)
        if state == "submitted":
            return FormOutcome(
                status="submitted", plan=plan, confirmation=evidence, screenshots=shots
            )
        if state == "failed":
            return FormOutcome(status="failed", plan=plan, error=evidence, screenshots=shots)
        return FormOutcome(
            status="failed",
            plan=plan,
            error=(
                "The form was submitted but the site did not confirm it. It may or may not "
                "have gone through: check the screenshot and the site before retrying."
            ),
            screenshots=shots,
        )
    except PlaywrightError as exc:
        return FormOutcome(status="failed", error=str(exc).splitlines()[0], screenshots=shots)
    finally:
        if not page.is_closed():
            page.close()


def assist(
    browser: Browser,
    url: str,
    book: AnswerBook,
    *,
    settings: Settings,
    wait_seconds: float = 900.0,
    screenshot_dir: Path | None = None,
    label: str = "application",
) -> FormOutcome:
    """Fill what is known in a visible browser and wait for *you* to submit.

    You are driving: this opens the page as your own browser session, fills in
    the answers you already gave, outlines what is still missing, and watches
    for the site's confirmation. It never clicks submit.
    """
    try:
        check_form_url(url, settings)
    except FormUrlRefused as exc:
        return FormOutcome(status="needs_human", blockers=[{"kind": "url", "detail": str(exc)}])

    page = browser.new_page()
    shots: list[str] = []
    try:
        _open(page, url)
        plan = build_plan(scan(page), book)
        failed = _fill_all(page, plan, book)
        for form_field in plan.unanswered:
            try:
                page.locator(form_field.ref).evaluate(
                    "(el, css) => el.setAttribute('style', (el.getAttribute('style') || '') + ';' + css)",
                    _HIGHLIGHT_CSS,
                )
            except PlaywrightError:
                continue
        if failed:
            log.info("assist: could not fill %s", "; ".join(failed))
        state, evidence = _await_outcome(
            page,
            page.url,
            wait_seconds,
            baseline=_confirm_phrases(_body(page)),
            fail_on_errors=False,
        )
        if state == "submitted":
            shot = _screenshot(page, screenshot_dir, f"{label}-after-submit.png")
            if shot:
                shots.append(shot)
            return FormOutcome(
                status="submitted", plan=plan, confirmation=evidence, screenshots=shots
            )
        return FormOutcome(
            status="needs_human",
            plan=plan,
            blockers=[
                *plan.blockers,
                {
                    "kind": "not_confirmed",
                    "detail": "No confirmation was seen before the window closed.",
                },
            ],
        )
    except PlaywrightError as exc:
        return FormOutcome(status="failed", error=str(exc).splitlines()[0], screenshots=shots)
    finally:
        if not page.is_closed():
            page.close()
