"""The life of an application: prepare, approve, send, track.

    preparing -> needs_answers | needs_review | needs_human | approved | skipped
    needs_review --(you approve)--> approved
    approved --(worker sends)--> submitting -> submitted | failed | needs_human
    submitted -> replied -> interviewing -> offer | rejected   (you move these)

Nothing reaches ``approved`` except through your click or the auto policy, and
nothing is sent from any other state. Every step is written to the
application's event log.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.apply import ledger
from jobportal.apply.answers import AnswerBook, count_uses, load_answers, save_answer
from jobportal.apply.email_apply import compose
from jobportal.apply.forms import filler
from jobportal.apply.mail import MailError, MailTransport, OutgoingMail, build_message, save_draft
from jobportal.apply.policy import auto_decision, email_send_allowed
from jobportal.browser import BrowserUnavailable, LazyBrowser
from jobportal.config import Channel, UserConfig
from jobportal.db import utcnow
from jobportal.http import PoliteClient
from jobportal.llm import LLM
from jobportal.models import (
    ACTIVE_STATUSES,
    SENT_STATUSES,
    Application,
    ApplicationEvent,
    AppStatus,
    Job,
    JobScore,
    OutboundEmail,
    SourceKind,
    User,
)
from jobportal.resume.service import build_resume
from jobportal.scoring import is_blocked
from jobportal.settings import Settings

log = logging.getLogger(__name__)

#: Sources whose hosted application forms the filler can read.
FORM_SOURCES = {SourceKind.greenhouse.value, SourceKind.lever.value, SourceKind.ashby.value}
#: States you can still change your mind in.
EDITABLE = {
    AppStatus.needs_review.value,
    AppStatus.needs_answers.value,
    AppStatus.needs_human.value,
}
#: Where the tracker may move an application once it is out.
STAGES = [
    AppStatus.submitted,
    AppStatus.replied,
    AppStatus.interviewing,
    AppStatus.offer,
    AppStatus.rejected,
    AppStatus.withdrawn,
]
INTERRUPTED_AFTER = timedelta(minutes=10)


class ApplicationError(Exception):
    """An action that is not allowed in the application's current state."""


def add_event(application: Application, kind: str, **detail: Any) -> None:
    application.events.append(ApplicationEvent(kind=kind, detail=detail, at=utcnow()))


def choose_channel(job: Job) -> Channel:
    if job.contact_email:
        return Channel.email
    kind = job.source.kind
    if job.apply_url and (kind in FORM_SOURCES or kind == SourceKind.manual.value):
        return Channel.form
    return Channel.manual


def _score_of(session: Session, user_id: int, job_id: int) -> JobScore | None:
    return session.scalar(
        select(JobScore).where(JobScore.user_id == user_id, JobScore.job_id == job_id)
    )


def _hard_stop(
    session: Session, config: UserConfig, user: User, job: Job, application_id: int | None
) -> dict[str, str] | None:
    """A reason this job must not be applied to at all."""
    blocked = is_blocked(job.company_name, config.search.blocked_companies)
    if blocked:
        return {"kind": "blocked_company", "detail": f"{job.company_name} is on your blocked list."}
    if is_blocked(job.client_name, config.search.blocked_companies):
        return {"kind": "blocked_company", "detail": f"{job.client_name} is on your blocked list."}
    if job.closed_at is not None:
        return {"kind": "closed", "detail": "The posting has been taken down."}
    twin = session.scalars(
        select(Application)
        .join(Job, Job.id == Application.job_id)
        .where(
            Application.user_id == user.id,
            Job.fingerprint == job.fingerprint,
            Application.job_id != job.id,
            Application.status.in_([s.value for s in ACTIVE_STATUSES]),
        )
        .order_by(Application.id)
    ).first()
    if twin is not None and twin.id != application_id:
        return {
            "kind": "duplicate",
            "detail": f"Same role as application #{twin.id} ({twin.job.location or 'another posting'}).",
        }
    return None


def _ledger_warning(
    session: Session,
    config: UserConfig,
    user: User,
    job: Job,
    application_id: int | None,
    now: datetime,
) -> dict[str, str] | None:
    client, vendor = ledger.parties(job)
    conflict = ledger.find_conflict(
        session,
        user.id,
        client=client,
        vendor=vendor,
        window_days=config.search.policy.ledger_window_days,
        now=now,
        exclude_application_id=application_id,
    )
    if conflict is not None:
        return {"kind": "ledger_conflict", "detail": ledger.describe(conflict)}
    if ledger.is_vendor_role(job) and not client:
        return {
            "kind": "client_unknown",
            "detail": "The vendor does not name the client, so a double submission cannot be ruled out.",
        }
    return None


# ------------------------------------------------------------------ prepare


def request_application(session: Session, user: User, job: Job) -> Application:
    """You asked to apply to this job: queue it for preparation."""
    application = session.scalar(
        select(Application).where(Application.user_id == user.id, Application.job_id == job.id)
    )
    if application is None:
        application = Application(
            user_id=user.id, job_id=job.id, channel=choose_channel(job).value,
            status=AppStatus.preparing.value,
        )  # fmt: skip
        session.add(application)
        add_event(application, "requested")
    elif application.status in (
        AppStatus.skipped.value,
        AppStatus.withdrawn.value,
        AppStatus.failed.value,
    ):
        application.status = AppStatus.preparing.value
        application.error = ""
        add_event(application, "requested_again")
    session.flush()
    return application


def prepare_application(
    session: Session,
    settings: Settings,
    config: UserConfig,
    user: User,
    job: Job,
    *,
    browser: LazyBrowser,
    client: PoliteClient | None = None,
    llm: LLM | None = None,
    now: datetime | None = None,
) -> Application:
    """Tailor the resume, work out exactly what would be sent, and decide who sends it.

    Sends nothing. The result is an application that is ready for your click
    (``needs_review``), cleared by the auto policy (``approved``), waiting on
    you for an answer or a hand-off, or ruled out (``skipped``).
    """
    now = now or utcnow()
    application = session.scalar(
        select(Application).where(Application.user_id == user.id, Application.job_id == job.id)
    )
    if application is not None and application.status in (
        {s.value for s in SENT_STATUSES} | {AppStatus.approved.value, AppStatus.submitting.value}
    ):
        return application  # already cleared or sent: never re-plan underneath it
    if application is None:
        application = Application(
            user_id=user.id, job_id=job.id, channel="", status=AppStatus.preparing.value
        )
        session.add(application)
        session.flush()

    application.channel = choose_channel(job).value
    application.blockers = []
    application.error = ""
    application.auto = False

    stop = _hard_stop(session, config, user, job, application.id)
    if stop is not None:
        application.status = AppStatus.skipped.value
        application.blockers = [stop]
        add_event(application, "skipped", reason=stop["kind"], detail=stop["detail"])
        session.flush()
        return application

    score = _score_of(session, user.id, job.id)
    lane = config.search.lane(score.lane if score else None) or config.search.lanes[0]
    try:
        variant = build_resume(
            session,
            settings,
            config,
            user,
            job,
            variant=lane.resume,
            extra_terms=[*lane.skills.core, *lane.skills.bonus],
            browser=browser.get(),
            llm=llm,
        )
    except BrowserUnavailable as exc:
        application.status = AppStatus.needs_human.value
        application.blockers = [{"kind": "browser", "detail": str(exc)}]
        session.flush()
        return application
    application.resume_variant_id = variant.id

    blockers: list[dict[str, str]] = []
    ready = False
    if application.channel == Channel.email.value:
        draft = compose(
            job, config.profile, variant, attach_docx=config.search.policy.email.attach_docx
        )
        client_name, vendor_name = ledger.parties(job)
        application.prepared = {**draft.to_prepared(), "client": client_name, "vendor": vendor_name}
        if not settings.smtp_configured:
            blockers.append(_save_draft_instead(settings, config, application))
        else:
            ready = True
    elif application.channel == Channel.form.value:
        assert job.apply_url is not None
        book = AnswerBook(
            config.profile, load_answers(session, user.id), Path(variant.pdf_path or "")
        )
        outcome = filler.prepare(
            browser.get(), job.apply_url, book, settings=settings, client=client
        )
        prepared = outcome.plan.to_prepared() if outcome.plan else {"channel": "form"}
        application.prepared = {**prepared, "url": job.apply_url, "resume": variant.pdf_path}
        blockers.extend(outcome.blockers)
        ready = outcome.status == "ready"
        if outcome.status == "needs_answers":
            application.status = AppStatus.needs_answers.value
    else:
        application.prepared = {"channel": "manual", "url": job.url, "resume": variant.pdf_path}
        blockers.append(
            {
                "kind": "manual",
                "detail": "This site's application needs you (an account or a multi-step form). "
                "Your tailored resume is ready to upload.",
            }
        )

    warning = _ledger_warning(session, config, user, job, application.id, now)
    notes: list[str] = []
    if blockers:
        application.status = AppStatus.needs_human.value
    elif ready:
        decision = auto_decision(
            session, user.id, config.search.policy, job, score, application.channel, now=now,
            exclude_application_id=application.id,
        )  # fmt: skip
        notes = decision.reasons
        if decision.allowed and warning is None:
            application.status = AppStatus.approved.value
            application.auto = True
            application.approved_at = now
        else:
            application.status = AppStatus.needs_review.value
    if warning is not None:
        blockers.append(warning)
    application.blockers = blockers
    application.prepared = {**application.prepared, "auto_notes": notes}
    add_event(
        application, "prepared", status=application.status, blockers=[b["kind"] for b in blockers]
    )
    session.flush()
    return application


def _save_draft_instead(
    settings: Settings, config: UserConfig, application: Application
) -> dict[str, str]:
    """No mail server configured: leave an .eml draft to send by hand."""
    detail = "Outgoing mail is not configured (JOBPORTAL_SMTP_HOST), so the app cannot send this."
    try:
        message = build_message(_outgoing(settings, config, application.prepared, bcc=False))
        path = save_draft(message, settings.outbox_dir, f"application-{application.id}")
        application.prepared = {**application.prepared, "draft_file": str(path)}
        detail += f" A draft was saved to {path}: open it in your mail client and send it."
    except MailError as exc:
        detail += f" No draft could be saved either: {exc}"
    return {"kind": "no_smtp", "detail": detail}


def _outgoing(
    settings: Settings, config: UserConfig, prepared: dict[str, Any], *, bcc: bool
) -> OutgoingMail:
    sender = settings.mail_from or config.profile.email
    return OutgoingMail(
        sender=sender,
        sender_name=config.profile.name,
        to=str(prepared.get("to", "")),
        subject=str(prepared.get("subject", "")),
        body=str(prepared.get("body", "")),
        attachments=[Path(a) for a in prepared.get("attachments", [])],
        bcc=[sender] if bcc else [],
        in_reply_to=str(prepared.get("in_reply_to", "")),
        references=str(prepared.get("references", "")),
    )


# -------------------------------------------------------------- your actions


def approve(application: Application, *, now: datetime | None = None) -> None:
    """Clear a reviewed application for sending."""
    if application.status != AppStatus.needs_review.value:
        raise ApplicationError(
            f"Only applications waiting for review can be approved (this one is {application.status})."
        )
    overridden = [b["kind"] for b in application.blockers or []]
    application.status = AppStatus.approved.value
    application.auto = False
    application.approved_at = now or utcnow()
    add_event(application, "approved", overrode=overridden)


def edit_draft(application: Application, *, subject: str, body: str) -> None:
    if (
        application.status != AppStatus.needs_review.value
        or application.channel != Channel.email.value
    ):
        raise ApplicationError("Only an email application waiting for review can be edited.")
    if not subject.strip() or not body.strip():
        raise ApplicationError("Subject and message cannot be empty.")
    application.prepared = {
        **application.prepared,
        "subject": subject.strip(),
        "body": body.strip() + "\n",
    }
    add_event(application, "draft_edited")


def provide_answers(session: Session, application: Application, answers: dict[str, str]) -> int:
    """Store your answers to the open questions and queue the application again."""
    if application.status != AppStatus.needs_answers.value:
        raise ApplicationError("This application is not waiting for answers.")
    questions = {q["key"]: q["label"] for q in (application.prepared or {}).get("unanswered", [])}
    saved = 0
    for key, text in answers.items():
        if key in questions and save_answer(session, application.user_id, questions[key], text):
            saved += 1
    if saved:
        application.status = AppStatus.preparing.value
        add_event(application, "answers_provided", count=saved)
    return saved


def dismiss(application: Application, reason: str = "") -> None:
    """Decide not to apply."""
    if application.status in {s.value for s in SENT_STATUSES} | {AppStatus.submitting.value}:
        raise ApplicationError("This application has already gone out; mark it withdrawn instead.")
    application.status = AppStatus.skipped.value
    application.blockers = [
        {"kind": "dismissed", "detail": reason or "You dismissed this application."}
    ]
    add_event(application, "dismissed", reason=reason)


def mark_submitted(
    session: Session,
    config: UserConfig,
    application: Application,
    *,
    now: datetime | None = None,
    note: str = "",
) -> None:
    """You sent it yourself (finished a form by hand, sent the draft). Record it."""
    if application.status in {s.value for s in SENT_STATUSES}:
        raise ApplicationError("This application is already recorded as sent.")
    now = now or utcnow()
    _finish(session, config, application, now=now, confirmation=note or "Marked as sent by you.")
    application.auto = False
    add_event(application, "marked_submitted", note=note)


def set_stage(
    application: Application, stage: str, *, now: datetime | None = None, note: str = ""
) -> None:
    """Move a sent application along the tracker."""
    if stage not in {s.value for s in STAGES}:
        raise ApplicationError(f"Unknown stage: {stage}")
    if application.status not in {s.value for s in SENT_STATUSES} | {AppStatus.withdrawn.value}:
        raise ApplicationError("Only an application that has gone out can be moved on the tracker.")
    previous, application.status = application.status, stage
    if stage in (AppStatus.rejected.value, AppStatus.withdrawn.value, AppStatus.offer.value):
        application.follow_up_at = None
        application.next_action = ""
    elif stage == AppStatus.replied.value:
        application.follow_up_at = None
        application.next_action = "Reply"
    elif stage == AppStatus.interviewing.value:
        application.follow_up_at = None
        application.next_action = "Prepare for the interview"
    if note:
        application.notes = (application.notes + "\n" if application.notes else "") + note
    add_event(application, "stage", previous=previous, to=stage, at=(now or utcnow()).isoformat())


def _finish(
    session: Session,
    config: UserConfig,
    application: Application,
    *,
    now: datetime,
    confirmation: str,
) -> None:
    policy = config.search.policy
    application.status = AppStatus.submitted.value
    application.submitted_at = now
    application.confirmation = confirmation
    application.error = ""
    application.blockers = []
    application.follow_up_at = now + timedelta(days=policy.follow_up_days)
    application.next_action = "Follow up if there is no reply"
    terms = config.profile.contract
    is_contract = application.job.employment_type == "contract"
    ledger.record(
        session,
        application,
        now=now,
        engagement="/".join(terms.engagements) if is_contract else "",
        rate=terms.rate if is_contract else "",
    )


# --------------------------------------------------------------------- send


def submit_application(
    session: Session,
    settings: Settings,
    config: UserConfig,
    application: Application,
    *,
    transport: MailTransport | None,
    browser: LazyBrowser,
    client: PoliteClient | None = None,
    now: datetime | None = None,
) -> str:
    """Send one approved application. Returns its resulting status, or ``deferred``.

    The state is committed as ``submitting`` *before* anything leaves, so a
    crash can never lead to the same application being sent twice: an
    interrupted send is surfaced for you to check instead of retried.
    """
    now = now or utcnow()
    if application.status != AppStatus.approved.value:
        raise ApplicationError(
            f"Only approved applications are sent (this one is {application.status})."
        )
    job = application.job
    user = session.get(User, application.user_id)
    assert user is not None

    stop = _hard_stop(session, config, user, job, application.id)
    if stop is not None:
        application.status = AppStatus.skipped.value
        application.blockers = [stop]
        add_event(application, "skipped", reason=stop["kind"], detail=stop["detail"])
        session.commit()
        return application.status

    if application.auto:
        # Re-check the unattended rules at the moment of sending.
        decision = auto_decision(
            session, user.id, config.search.policy, job, _score_of(session, user.id, job.id),
            application.channel, now=now, exclude_application_id=application.id,
        )  # fmt: skip
        warning = _ledger_warning(session, config, user, job, application.id, now)
        if not decision.allowed or warning is not None:
            application.status = AppStatus.needs_review.value
            application.auto = False
            application.prepared = {**application.prepared, "auto_notes": decision.reasons}
            if warning is not None:
                application.blockers = [warning]
            add_event(application, "returned_to_review", reasons=decision.reasons)
            session.commit()
            return application.status

    if application.channel == Channel.email.value:
        return _send_email(session, settings, config, application, transport, now)
    if application.channel == Channel.form.value:
        return _send_form(session, settings, config, application, browser, client, now)
    application.status = AppStatus.needs_human.value
    session.commit()
    return application.status


def _send_email(
    session: Session,
    settings: Settings,
    config: UserConfig,
    application: Application,
    transport: MailTransport | None,
    now: datetime,
) -> str:
    already = session.scalar(
        select(OutboundEmail).where(
            OutboundEmail.application_id == application.id, OutboundEmail.status == "sent"
        )
    )
    if already is not None:  # belt and braces: one application, one email
        _finish(
            session,
            config,
            application,
            now=already.sent_at or now,
            confirmation=f"Sent to {already.to_addr}",
        )
        session.commit()
        return application.status
    if transport is None:
        application.status = AppStatus.needs_human.value
        application.blockers = [{"kind": "no_smtp", "detail": "Outgoing mail is not configured."}]
        session.commit()
        return application.status

    allowed, why = email_send_allowed(session, config.search.policy, now=now)
    if not allowed:
        log.info("application %s deferred: %s", application.id, why)
        return "deferred"

    try:
        mail = _outgoing(
            settings, config, application.prepared, bcc=config.search.policy.email.bcc_self
        )
        message = build_message(mail)
    except MailError as exc:
        return _fail(session, application, str(exc))

    outbound = OutboundEmail(
        application_id=application.id,
        to_addr=mail.to,
        subject=str(message["Subject"]),
        body=mail.body,
        attachments=[str(path) for path in mail.attachments],
        message_id=str(message["Message-ID"]),
        in_reply_to=mail.in_reply_to,
        status="queued",
    )
    session.add(outbound)
    application.status = AppStatus.submitting.value
    application.attempts += 1
    session.commit()  # recorded as in flight before anything leaves

    try:
        transport.send(message, mail.recipients())
    except MailError as exc:
        outbound.status = "failed"
        outbound.error = str(exc)
        return _fail(session, application, str(exc))

    outbound.status = "sent"
    outbound.sent_at = now
    _finish(
        session,
        config,
        application,
        now=now,
        confirmation=f"Sent to {mail.to} (Message-ID {outbound.message_id})",
    )
    add_event(application, "submitted", channel="email", to=mail.to, auto=application.auto)
    session.commit()
    return application.status


def _send_form(
    session: Session,
    settings: Settings,
    config: UserConfig,
    application: Application,
    browser: LazyBrowser,
    client: PoliteClient | None,
    now: datetime,
) -> str:
    variant = application.resume_variant
    url = (application.prepared or {}).get("url") or application.job.apply_url
    if variant is None or not variant.pdf_path or not Path(variant.pdf_path).exists() or not url:
        return _fail(
            session,
            application,
            "The tailored resume or the application address is missing; prepare it again.",
        )
    book = AnswerBook(
        config.profile, load_answers(session, application.user_id), Path(variant.pdf_path)
    )

    application.status = AppStatus.submitting.value
    application.attempts += 1
    session.commit()  # recorded as in flight before anything leaves

    try:
        outcome = filler.submit(
            browser.get(),
            url,
            book,
            settings=settings,
            client=client,
            screenshot_dir=settings.screenshots_dir,
            label=f"application-{application.id}",
        )
    except BrowserUnavailable as exc:
        return _fail(session, application, str(exc))

    prepared = dict(application.prepared or {})
    if outcome.plan is not None:
        prepared.update(outcome.plan.to_prepared())
    prepared["screenshots"] = outcome.screenshots
    application.prepared = prepared

    if outcome.status == "submitted":
        _finish(session, config, application, now=now, confirmation=outcome.confirmation)
        if outcome.plan is not None:
            used = [p.field.key for p in outcome.plan.fill if p.resolution.source == "answer bank"]
            count_uses(session, application.user_id, used)
        add_event(application, "submitted", channel="form", auto=application.auto)
    elif outcome.status == "needs_answers":
        application.status = AppStatus.needs_answers.value
        add_event(application, "needs_answers")
    elif outcome.status == "needs_human":
        application.status = AppStatus.needs_human.value
        application.blockers = outcome.blockers
        add_event(application, "needs_human", blockers=[b["kind"] for b in outcome.blockers])
    else:
        return _fail(session, application, outcome.error or "The form could not be submitted.")
    session.commit()
    return application.status


def _fail(session: Session, application: Application, error: str) -> str:
    application.status = AppStatus.failed.value
    application.error = error
    add_event(application, "failed", error=error)
    session.commit()
    return application.status


def recover_interrupted(session: Session, *, now: datetime | None = None) -> int:
    """Surface applications left mid-send by a crash. They are never resent automatically."""
    now = now or utcnow()
    stuck = session.scalars(
        select(Application).where(
            Application.status == AppStatus.submitting.value,
            Application.updated_at < now - INTERRUPTED_AFTER,
        )
    ).all()
    for application in stuck:
        application.status = AppStatus.failed.value
        application.error = (
            "Sending was interrupted and it is not known whether this went out. Check your sent "
            "mail (or the site) and then either mark it as sent or prepare it again."
        )
        add_event(application, "interrupted")
    return len(stuck)
