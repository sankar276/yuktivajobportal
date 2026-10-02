"""Fill and submit an application form in a real browser.

Three modes, in increasing order of trust:

``prepare``  Open the page, read the form, decide what would go where. Nothing
             is typed and nothing is sent.
``assist``   A visible browser: everything known is filled in, and *you*
             review, complete and submit. For forms behind a bot check or
             with anything the filler cannot handle.
``submit``   Unattended: fill and submit. Refused unless the form has no bot
             check, no login wall, no unanswered required question and no
             control the filler cannot operate, and unless the form is still
             what was approved.

The browser is not disguised and bot checks are never worked around: a form
that runs one is yours to submit. A submission only counts as sent when the
site plainly confirms it; a click with no confirmation is reported as
``unconfirmed`` (it may have gone through), never as sent and never as
"nothing happened".
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, Request, Route
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from jobportal import egress
from jobportal.apply.answers import AnswerBook
from jobportal.apply.forms.fields import FieldKind, FormField, Resolution, to_dict
from jobportal.http import PoliteClient
from jobportal.netguard import UrlRefused, behind_proxy, check_public_url, is_local_url
from jobportal.settings import Settings

log = logging.getLogger(__name__)

SCAN_JS = (Path(__file__).parent / "scan.js").read_text(encoding="utf-8")
NAVIGATION_TIMEOUT_MS = 30_000
OUTCOME_TIMEOUT_S = 25.0
# Wording that says *this application* arrived. A bare "thank you" is not
# enough: forms say that in their introductions and on error pages too.
_CONFIRM_RE = re.compile(
    r"thank(?:s| you) for (?:applying|your application|submitting your application)"
    r"|(?:your )?application (?:has been|was|is) (?:successfully )?(?:received|submitted|sent)"
    r"|we(?:'ve| have) (?:successfully )?received your application"
    r"|(?:you(?:'ve| have) )?successfully (?:submitted|applied)"
    r"|application (?:submitted|received|sent)\b"
    r"|your application is on its way",
    re.IGNORECASE,
)
# Wording that takes a confirmation back, or says there is still something to do.
_NOT_YET_RE = re.compile(
    r"\bnot (?:yet )?(?:been )?(?:sent|submitted|received|completed?|saved)\b"
    r"|\bone more step\b|\balmost (?:done|there|finished)\b"
    r"|\b(?:complete|solve|pass) the (?:captcha|verification|challenge)\b"
    r"|\bto (?:complete|finish|submit) your application\b"
    r"|\bsomething went wrong\b|\btry again\b|\bnothing was saved\b"
    r"|\berror \d{3}\b|\bserver error\b|\bcould not (?:be )?(?:submit|sen[dt]|sav|process)",
    re.IGNORECASE,
)
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
    #: Optional, unanswered and empty: left blank.
    left_blank: list[FormField] = field(default_factory=list)
    #: Optional, unanswered, and already set by the site: left as the site set it.
    kept: list[FormField] = field(default_factory=list)
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
            "kept": [
                {"label": form_field.label or form_field.name, "value": form_field.current}
                for form_field in self.kept
            ],
            "submit_text": self.submit_text,
            "plan_hash": self.fingerprint(),
        }

    def fingerprint(self) -> str:
        """Identifies what would be entered. An approval is for one fingerprint.

        The form is read again at the moment of sending. If the site changed
        its questions meanwhile, or a stored answer changed, this no longer
        matches what was approved and the application goes back for review.
        """

        def shown(planned: PlannedField) -> str:
            value = planned.resolution.value
            return Path(value).name if planned.field.type == "file" else value

        payload = {
            "fill": sorted([p.field.key, p.field.type, shown(p)] for p in self.fill),
            "kept": sorted([f.key, f.current] for f in self.kept),
            "blank": sorted(f.key for f in self.left_blank),
            "unanswered": sorted(f.key for f in self.unanswered),
            "submit": self.submit_text,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class FormOutcome:
    #: ready | needs_answers | needs_human | changed | submitted | unconfirmed | failed
    #: ``changed``: the form is no longer what was approved; nothing was typed.
    #: ``unconfirmed``: submit was pressed and the site did not confirm.
    #: ``failed``: nothing was sent.
    status: str
    plan: FormPlan | None = None
    confirmation: str = ""
    error: str = ""
    screenshots: list[str] = field(default_factory=list)
    blockers: list[dict[str, str]] = field(default_factory=list)


# -------------------------------------------------------------------- guard


def check_form_url(url: str, settings: Settings) -> None:
    """Only public https pages; local addresses only when explicitly allowed."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise FormUrlRefused(f"not a valid address: {url!r}") from exc
    # Browsers read "https://a\\@b/" differently from URL parsers; an address
    # like that (or one with a user@ part) is never opened.
    if "\\" in url or parts.username is not None or parts.password is not None:
        raise FormUrlRefused("the address contains a backslash or a user name")
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in url):
        raise FormUrlRefused("the address contains spaces or control characters")
    try:
        check_public_url(url, allow_local=settings.allow_local_addresses, require_https=True)
    except UrlRefused as exc:
        raise FormUrlRefused(str(exc)) from exc


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _off_site(page: Page, allowed_hosts: frozenset[str] | None) -> dict[str, str] | None:
    """A blocker when the browser is not on one of the hosts it was sent to.

    Checked after the page has loaded and again just before submitting, so a
    redirect (or a script that navigates) cannot get a form on some other
    site read, filled or submitted.
    """
    if allowed_hosts is None:
        return None
    host = _host(page.url)
    if host in allowed_hosts:
        return None
    return {
        "kind": "redirected",
        "detail": (
            f"The application page led to another site ({host or 'unknown'}), "
            "so it was not read or filled in. Open it yourself."
        ),
    }


def _new_page(browser: Browser, settings: Settings) -> tuple[BrowserContext, Page]:
    """A fresh, isolated browser context that can only reach the public internet.

    Two guards. Every connection the context makes goes through the egress
    proxy, which looks the name up once, refuses anything that is not a
    public address and connects to the address it checked: that covers what
    no page-level hook sees (redirect hops, popups, workers) and a name that
    answers differently the second time. On top of it, requests to a local
    address are aborted before they are sent. Behind an outbound proxy of your
    own only the second guard applies, as the proxy does the connecting.
    """
    guarded = not settings.allow_local_addresses
    if guarded and not behind_proxy():
        # "<-loopback>": even localhost goes through the proxy, to be refused there.
        context = browser.new_context(
            proxy={"server": egress.shared().url, "bypass": "<-loopback>"}
        )
    else:
        context = browser.new_context()
    page = context.new_page()
    # An application form has no business opening further windows.
    context.on("page", lambda other: other.close() if other is not page else None)
    if guarded:

        def handler(route: Route, request: Request) -> None:
            if is_local_url(request.url):
                route.abort("blockedbyclient")
            else:
                route.continue_()

        # On the context, so that it also covers anything the page spawns.
        context.route("**/*", handler)
        context.route_web_socket(
            "**/*",
            lambda socket: (
                socket.close()
                if is_local_url(re.sub(r"^ws", "http", socket.url))
                else socket.connect_to_server()
            ),
        )
    return context, page


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
            label_source=str(item.get("labelSource") or ""),
            current=str(item.get("current") or ""),
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
        elif form_field.required:
            if form_field.type == "file":
                plan.blockers.append(
                    {
                        "kind": "unsupported",
                        "detail": f"The form requires an upload this app does not produce: {form_field.label or form_field.name}",
                    }
                )
            elif not form_field.label:
                plan.blockers.append(
                    {
                        "kind": "unlabelled",
                        "detail": "The form has a required question whose wording could not be read, so it is yours to fill in.",
                    }
                )
            else:
                # Also when the site pre-selected something: a default the
                # site chose is not an answer you gave.
                plan.unanswered.append(form_field)
        elif form_field.prefilled:
            plan.kept.append(form_field)
        else:
            plan.left_blank.append(form_field)

    if not plan.submit_ref:
        several = int(scanned.get("submitCandidates") or 0) > 1
        plan.blockers.append(
            {
                "kind": "no_submit",
                "detail": (
                    "More than one button could submit this form, so it is yours to submit."
                    if several
                    else "No submit button was found (the form may have several steps)."
                ),
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


def _still_there(page: Page, ref: str | None) -> bool:
    """Is the control we tagged before submitting still on show?"""
    if not ref:
        return False
    try:
        locator = page.locator(ref)
        return locator.count() > 0 and locator.first.is_visible()
    except PlaywrightError:
        return True  # mid-navigation: do not read that as "gone"


def _await_outcome(
    page: Page,
    timeout_s: float,
    *,
    baseline: set[str],
    form_ref: str | None,
    fail_on_errors: bool,
) -> tuple[str, str]:
    """``("submitted", evidence)``, ``("failed", why)`` or ``("unknown", "")``.

    "Submitted" needs all of: wording that says the application arrived, which
    was not on the page before; the form itself gone (``form_ref`` is its
    submit button, or failing that one of its fields); nothing on the page
    taking it back ("not been sent yet", an error page); and no bot check or
    login wall in the way. Anything less is "unknown".

    ``baseline`` holds confirmation-like phrases already on the page before it
    was submitted, so that a form's own introduction is not mistaken for one.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if page.is_closed():
            return "unknown", ""
        try:
            body = page.inner_text("body", timeout=2_000)
        except PlaywrightError:
            time.sleep(0.3)  # mid-navigation
            continue
        form_present = _still_there(page, form_ref)
        fresh = _confirm_phrases(body) - baseline
        if fresh and not form_present and not _NOT_YET_RE.search(body[:6000]):
            try:
                state = scan(page)
            except PlaywrightError:
                time.sleep(0.3)
                continue
            if not (state.get("captcha") or state.get("login") or state.get("interstitial")):
                match = next(m for m in _CONFIRM_RE.finditer(body) if m.group(0).lower() in fresh)
                start = max(0, match.start() - 60)
                return "submitted", " ".join(body[start : match.end() + 160].split())
        if fail_on_errors and form_present:
            errors = _visible_errors(page)
            if errors:
                return "failed", "The form reported: " + "; ".join(errors)
        time.sleep(0.4)
    return "unknown", ""


# -------------------------------------------------------------------- modes


def _robots_blocker(url: str, client: PoliteClient | None) -> dict[str, str] | None:
    """Unattended page loads honour robots.txt, like every other automated request."""
    if client is None:
        return None
    try:
        if client.allowed(url):
            return None
        unreachable = client.robots_for(url).unreachable
    except Exception as exc:  # a robots lookup must never crash a run
        log.warning("robots check failed for %s: %s", url, exc)
        unreachable = True
    if unreachable:
        detail = (
            "The site's robots.txt could not be read just now, so the page was not opened "
            "automatically. Prepare it again later, or open it yourself."
        )
    else:
        detail = (
            "The site's robots.txt asks automated clients not to open this page, "
            "so it is yours to open."
        )
    return {"kind": "robots", "detail": detail}


def _refusal(
    url: str, settings: Settings, client: PoliteClient | None, *, robots: bool = True
) -> FormOutcome | None:
    """Reasons not to open the page at all."""
    try:
        check_form_url(url, settings)
    except FormUrlRefused as exc:
        return FormOutcome(status="needs_human", blockers=[{"kind": "url", "detail": str(exc)}])
    blocked = _robots_blocker(url, client) if robots else None
    if blocked:
        return FormOutcome(status="needs_human", blockers=[blocked])
    return None


def prepare(
    browser: Browser,
    url: str,
    book: AnswerBook,
    *,
    settings: Settings,
    client: PoliteClient | None = None,
    allowed_hosts: frozenset[str] | None = None,
) -> FormOutcome:
    """Read the form and plan the answers. Types nothing, sends nothing.

    ``allowed_hosts``: the sites the page may turn out to be on; a redirect
    anywhere else is not read.
    """
    refused = _refusal(url, settings, client)
    if refused:
        return refused

    context, page = _new_page(browser, settings)
    try:
        _open(page, url)
        elsewhere = _off_site(page, allowed_hosts)
        if elsewhere:
            return FormOutcome(status="needs_human", blockers=[elsewhere])
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
        context.close()
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
    allowed_hosts: frozenset[str] | None = None,
    expect_plan: str | None = None,
) -> FormOutcome:
    """Fill and submit unattended. Refuses unless the form is fully automatable.

    ``expect_plan`` is the fingerprint of the plan that was approved. If the
    form as it is now would be filled differently, nothing is typed and the
    outcome is ``changed``.
    """
    refused = _refusal(url, settings, client)
    if refused:
        return refused

    context, page = _new_page(browser, settings)
    shots: list[str] = []
    plan: FormPlan | None = None
    pressed = False
    try:
        _open(page, url)
        elsewhere = _off_site(page, allowed_hosts)
        if elsewhere:
            return FormOutcome(status="needs_human", blockers=[elsewhere])
        plan = build_plan(scan(page), book)
        if plan.status != "ready":
            return FormOutcome(status=plan.status, plan=plan, blockers=list(plan.blockers))
        if expect_plan is not None and plan.fingerprint() != expect_plan:
            detail = (
                "The form, or one of your stored answers, changed after this was approved. "
                "Nothing was entered. Check what would be sent now and approve it again."
            )
            return FormOutcome(
                status="changed", plan=plan, blockers=[{"kind": "form_changed", "detail": detail}]
            )

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
        elsewhere = _off_site(page, allowed_hosts)
        if elsewhere:
            return FormOutcome(status="needs_human", plan=plan, blockers=[elsewhere])

        shot = _screenshot(page, screenshot_dir, f"{label}-filled.png")
        if shot:
            shots.append(shot)
        baseline = _confirm_phrases(_body(page))
        assert plan.submit_ref is not None
        pressed = True
        page.locator(plan.submit_ref).click()
        state, evidence = _await_outcome(
            page,
            OUTCOME_TIMEOUT_S,
            baseline=baseline,
            form_ref=plan.submit_ref,
            fail_on_errors=True,
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
            status="unconfirmed",
            plan=plan,
            error=(
                "The submit button was pressed but the site did not confirm the application. "
                "It may or may not have gone through: look at the screenshot and at the site, "
                "then mark it as sent or prepare it again."
            ),
            screenshots=shots,
        )
    except PlaywrightError as exc:
        message = str(exc).splitlines()[0]
        if pressed:
            # The click happened; what the site did with it is not known.
            return FormOutcome(
                status="unconfirmed",
                plan=plan,
                error=(
                    f"The page failed after the submit button was pressed ({message}). The "
                    "application may or may not have gone through: check the site, then mark "
                    "it as sent or prepare it again."
                ),
                screenshots=shots,
            )
        return FormOutcome(status="failed", plan=plan, error=message, screenshots=shots)
    finally:
        context.close()


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
    refused = _refusal(url, settings, None, robots=False)
    if refused:
        return refused

    context, page = _new_page(browser, settings)
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
        # The form counts as gone when the control it was recognised by is gone.
        anchor = plan.submit_ref or next(
            (item.ref for item in [*(p.field for p in plan.fill), *plan.unanswered]), None
        )
        state, evidence = _await_outcome(
            page,
            wait_seconds,
            baseline=_confirm_phrases(_body(page)),
            form_ref=anchor,
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
        context.close()
