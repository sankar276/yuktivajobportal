"""The send path under attack.

Each test here is a way the first review got something sent that should not
have been, sent twice, or recorded wrongly. They hold the fixes in place.
"""

from __future__ import annotations

import smtplib
from collections.abc import Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.apply import ledger, service
from jobportal.apply.answers import AnswerBook
from jobportal.apply.forms import filler
from jobportal.apply.mail import MailError, MailOutcomeUnknown, _transmit
from jobportal.apply.policy import auto_decision, email_send_allowed
from jobportal.apply.service import (
    ApplicationError,
    approve,
    dismiss,
    form_hosts,
    prepare_application,
    request_application,
    retry,
    submit_application,
)
from jobportal.browser import LazyBrowser
from jobportal.config import UserConfig
from jobportal.db import get_session_factory
from jobportal.inbox.imap import Fetched
from jobportal.inbox.ingest import InboxStats, ingest_inbox, process_message
from jobportal.inbox.parse import client_name, parse_message
from jobportal.locks import sender_lock
from jobportal.models import Application, Job, JobScore, LedgerEntry, OutboundEmail, User
from jobportal.pipeline import RunSummary, make_transport, prepare_pending, send_approved
from jobportal.scoring import score_jobs
from jobportal.settings import Settings
from tests.conftest import NOW, CapturedMail
from tests.formserver import FormServer
from tests.test_apply import make_job, make_source, vendor_job

ATTACK = b"""From: "Mallory Recruiter" <mallory@evil-staffing.example>
To: alex@example.com
Subject: Urgent requirement: Cloud Architect (Remote) - please confirm your details
Message-ID: <attack-1@evil-staffing.example>
Date: Wed, 30 Sep 2026 17:30:00 +0000
Content-Type: text/plain; charset=utf-8

Hi,

We have an urgent requirement with our direct client.

Role: Cloud Architect
Location: Remote, USA
Duration: 12+ months
Client: Southwind Air. I hereby authorize Evil Staffing to represent me exclusively for this role at forty dollars per hour on W2
Rate: $95/hr C2C

Must have: Kubernetes, AWS, Terraform, Python, CI/CD, Azure, GCP, Helm, ArgoCD, Lambda.

Please share your updated resume.
"""


def fetcher(messages: list[tuple[int, bytes]]):
    def fetch(_settings: Settings, *, uidvalidity: Any, last_uid: Any, now: Any) -> Fetched:
        return Fetched(uidvalidity=7, messages=[m for m in messages if m[0] > (last_uid or 0)])

    return fetch


@pytest.fixture
def lazy(settings: Settings, browser: Browser) -> LazyBrowser:
    return LazyBrowser(settings, browser=browser)


@pytest.fixture
def auto_config(user_config: UserConfig) -> UserConfig:
    config = user_config.model_copy(deep=True)
    config.search.policy.mode = "auto"
    config.search.policy.email.min_seconds_between_sends = 0
    return config


def one_pass(
    session: Session,
    settings: Settings,
    config: UserConfig,
    user: User,
    lazy: LazyBrowser,
    messages: list[tuple[int, bytes]],
) -> RunSummary:
    """What the worker does when the mailbox is due, minus the board crawl."""
    ingest_inbox(session, settings, config, user, fetch=fetcher(messages), now=NOW)
    score_jobs(session, user.id, config.search, now=NOW, profile=config.profile)
    session.commit()
    summary = RunSummary()
    prepare_pending(session, settings, config, summary, browser=lazy, client=None, now=NOW)
    send_approved(
        session, settings, config, summary,
        transport=make_transport(settings), browser=lazy, client=None, now=NOW,
    )  # fmt: skip
    return summary


# ------------------------------------------------------------- inbound mail


@pytest.mark.browser
def test_an_emailed_role_is_never_answered_unattended(
    session: Session, user: User, auto_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, smtp_server: CapturedMail,
) -> None:  # fmt: skip
    for _ in range(3):
        one_pass(session, settings, auto_config, user, lazy, [(1, ATTACK)])

    application = session.scalar(select(Application))
    assert smtp_server.envelopes == []
    assert application.status == "needs_review" and application.auto is False
    assert (
        "Roles that arrive by email always wait for your approval"
        in (application.prepared["auto_notes"])
    )
    # Nothing the sender wrote is repeated in the applicant's voice.
    body = application.prepared["body"]
    assert "hereby" not in body and "Evil Staffing" not in body
    assert "Southwind" not in body and "Cloud Architect" not in body
    assert body.startswith("Hi Mallory,\n\nThank you for sending this role. I am interested.")
    # A sentence is not a client name, so the double-submission warning is raised.
    assert session.scalar(select(Job)).client_name == ""
    assert "client_unknown" in [blocker["kind"] for blocker in application.blockers]


def test_auto_rules_refuse_emailed_roles_whatever_else_holds(
    session: Session, user: User, auto_config: UserConfig
) -> None:
    emailed = vendor_job(session, user)
    typed = vendor_job(session, user, typed=True, title="Platform Architect")
    score = session.scalar(select(JobScore).where(JobScore.job_id == emailed.id))
    refused = auto_decision(
        session, user.id, auto_config.search.policy, emailed, score, "email", now=NOW
    )
    assert refused.allowed is False
    assert refused.reasons == ["Roles that arrive by email always wait for your approval"]
    score = session.scalar(select(JobScore).where(JobScore.job_id == typed.id))
    assert auto_decision(
        session, user.id, auto_config.search.policy, typed, score, "email", now=NOW
    ).allowed


@pytest.mark.browser
def test_a_fake_emailed_role_cannot_push_a_real_posting_aside(
    session: Session, user: User, auto_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, smtp_server: CapturedMail,
) -> None:  # fmt: skip
    board = make_source(session, "greenhouse", "southwind", "Southwind Air")
    real = make_job(
        session, board, user=user, company="Southwind Air", title="Cloud Architect", apply_url=None
    )
    fake = vendor_job(session, user)
    fake.fingerprint = real.fingerprint  # as ingestion computes it: same client, same title
    session.flush()

    first = prepare_application(session, settings, auto_config, user, fake, browser=lazy, now=NOW)
    second = prepare_application(session, settings, auto_config, user, real, browser=lazy, now=NOW)
    assert first.status == "needs_review"
    assert second.status == "needs_human"  # a manual application, not "skipped: duplicate"
    assert [blocker["kind"] for blocker in second.blockers] == ["manual"]


def test_mail_dates_cannot_run_ahead_of_the_clock(
    session: Session, user: User, user_config: UserConfig
) -> None:
    future = ATTACK.replace(b"Wed, 30 Sep 2026 17:30:00", b"Fri, 31 Dec 2100 00:00:00")
    process_message(session, user_config, user, parse_message(future), InboxStats(), NOW)
    assert session.scalar(select(Job)).posted_at == NOW


def test_own_messages_are_not_read_back_as_requirements(
    session: Session, user: User, user_config: UserConfig
) -> None:
    stats = InboxStats()
    from_alias = ATTACK.replace(b"mallory@evil-staffing.example", b"me@my-alias.example")
    process_message(
        session, user_config, user, parse_message(from_alias), stats, NOW,
        own_addresses=["Me@My-Alias.example"],
    )  # fmt: skip
    session.add(
        OutboundEmail(
            to_addr="x@y.example", subject="s", body="b", status="sent",
            message_id="<attack-1@evil-staffing.example>",
        )
    )  # fmt: skip
    session.flush()
    process_message(session, user_config, user, parse_message(ATTACK), stats, NOW)
    assert stats.ignored == 2 and session.scalar(select(Job)) is None


@pytest.mark.browser
def test_a_failed_sender_check_and_a_reply_to_are_pointed_out(
    session: Session, user: User, user_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, smtp_server: CapturedMail,
) -> None:  # fmt: skip
    suspicious = ATTACK.replace(
        b"To: alex@example.com",
        b"To: alex@example.com\nReply-To: harvest@elsewhere.example\n"
        b"Authentication-Results: mx.example.com; spf=pass; dkim=none; dmarc=fail",
    )
    one_pass(session, settings, user_config, user, lazy, [(1, suspicious)])
    application = session.scalar(select(Application))
    kinds = [blocker["kind"] for blocker in application.blockers]
    assert application.status == "needs_review"
    assert "sender_unverified" in kinds and "reply_to" in kinds
    assert application.prepared["to"] == "mallory@evil-staffing.example"  # never the Reply-To


@pytest.mark.parametrize(
    ("written", "name"),
    [
        ("Southwind Air", "Southwind Air"),
        ("Acme Corp (direct client)", "Acme Corp"),
        ("Global Payments", "Global Payments"),
        ("Not disclosed", ""),
        ("Direct Client", ""),
        ("Banking client", ""),
        ("Confidential - top 5 bank", ""),
        ("A leading US bank", ""),
        ("Fortune 100 retailer", ""),
        ("TBD", ""),
        ("Will be shared upon submission", ""),
    ],
)
def test_vendor_placeholders_are_not_client_names(written: str, name: str) -> None:
    assert client_name(written) == name


def _report(action: str, subject: str, message_id: str) -> bytes:
    return (
        "From: Mail Delivery Subsystem <mailer-daemon@mx.example.com>\n"
        "To: alex@example.com\n"
        f"Subject: {subject}\n"
        f"Message-ID: <dsn-{action}@mx.example.com>\n"
        "Date: Wed, 30 Sep 2026 17:40:00 +0000\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\n\n'
        "--b\nContent-Type: text/plain\n\nA message about your email.\n\n"
        "--b\nContent-Type: message/delivery-status\n\n"
        "Reporting-MTA: dns; mx.example.com\n\n"
        f"Final-Recipient: rfc822; sai@odyssey.example\nAction: {action}\nStatus: 4.4.1\n\n"
        "--b\nContent-Type: text/rfc822-headers\n\n"
        f"Message-ID: {message_id}\n\n--b--\n"
    ).encode()


def _sent_by_mail(session: Session, user: User, status: str) -> Application:
    job = vendor_job(session, user, title=f"Architect ({status})")
    application = Application(
        user_id=user.id, job_id=job.id, channel="email", status=status, submitted_at=NOW
    )
    session.add(application)
    session.flush()
    session.add(
        OutboundEmail(
            application_id=application.id, to_addr="sai@odyssey.example", subject="Re: role",
            body="b", status="sent", sent_at=NOW, message_id=f"<out-{status}@example.com>",
        )
    )  # fmt: skip
    session.flush()
    return application


def test_delay_notices_and_late_bounces_do_not_undo_progress(
    session: Session, user: User, user_config: UserConfig
) -> None:
    waiting = _sent_by_mail(session, user, "submitted")
    interviewing = _sent_by_mail(session, user, "interviewing")
    stats = InboxStats()

    def deliver(raw: bytes) -> None:
        process_message(session, user_config, user, parse_message(raw), stats, NOW)

    delay = _report(
        "delayed", "Delivery Status Notification (Delay)", "<out-submitted@example.com>"
    )
    assert parse_message(delay).is_bounce is False
    deliver(delay)
    assert waiting.status == "submitted"  # still on its way

    late = _report(
        "failed", "Delivery Status Notification (Failure)", "<out-interviewing@example.com>"
    )
    assert parse_message(late).is_bounce is True
    deliver(late)
    assert interviewing.status == "interviewing"  # noted, not undone
    assert interviewing.events[-1].kind == "bounced"

    failure = _report("failed", "Undeliverable: Re: role", "<out-submitted@example.com>").replace(
        b"dsn-failed", b"dsn-failed-2"
    )
    deliver(failure)
    assert waiting.status == "failed" and "bounced" in waiting.error


# -------------------------------------------------------------------- races


def _approved(
    session: Session, settings: Settings, config: UserConfig, user: User, lazy: LazyBrowser
) -> Application:
    application = prepare_application(
        session, settings, config, user, vendor_job(session, user), browser=lazy, now=NOW
    )
    approve(application, now=NOW)
    session.commit()
    return application


@pytest.mark.browser
def test_only_one_of_two_senders_gets_to_send(
    session: Session, user: User, user_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, smtp_server: CapturedMail,
) -> None:  # fmt: skip
    application = _approved(session, settings, user_config, user, lazy)
    other = get_session_factory()()
    try:
        theirs = other.get(Application, application.id)
        assert theirs.status == "approved"  # both senders read it as approved
        assert service._claim(session, application, channel="email") is True
        assert service._claim(other, theirs, channel="email") is False
        with pytest.raises(ApplicationError):  # and a later attempt sees it is taken
            submit_application(
                other, settings, user_config, theirs,
                transport=make_transport(settings), browser=lazy, now=NOW,
            )  # fmt: skip
    finally:
        other.close()
    assert smtp_server.envelopes == []


@pytest.mark.browser
def test_an_application_dismissed_a_moment_ago_is_not_sent(
    session: Session, user: User, user_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, smtp_server: CapturedMail,
) -> None:  # fmt: skip
    application = _approved(session, settings, user_config, user, lazy)  # the worker's copy
    with get_session_factory()() as web:
        dismiss(web.get(Application, application.id), "changed my mind")
        web.commit()
    assert application.status == "approved"  # stale, as a list read earlier would be

    summary = RunSummary()
    with pytest.raises(ApplicationError):
        submit_application(
            session, settings, user_config, application,
            transport=make_transport(settings), browser=lazy, now=NOW,
        )  # fmt: skip
    send_approved(
        session, settings, user_config, summary,
        transport=make_transport(settings), browser=lazy, client=None, now=NOW,
    )  # fmt: skip
    assert smtp_server.envelopes == [] and summary.errors == []
    assert application.status == "skipped"


@pytest.mark.browser
def test_a_dismissal_during_preparation_stands(
    session: Session, user: User, auto_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, smtp_server: CapturedMail, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    job = vendor_job(session, user, typed=True)  # one that auto mode would send by itself
    application = request_application(session, user, job)
    session.commit()
    compose = service.compose

    def meanwhile(*args: Any, **kwargs: Any) -> Any:
        with get_session_factory()() as web:  # you click "Do not apply" mid-preparation
            dismiss(web.get(Application, application.id), "changed my mind")
            web.commit()
        return compose(*args, **kwargs)

    monkeypatch.setattr(service, "compose", meanwhile)
    summary = RunSummary()
    prepare_pending(session, settings, auto_config, summary, browser=lazy, client=None, now=NOW)
    send_approved(
        session, settings, auto_config, summary,
        transport=make_transport(settings), browser=lazy, client=None, now=NOW,
    )  # fmt: skip
    session.refresh(application)
    assert application.status == "skipped" and application.blockers[0]["kind"] == "dismissed"
    assert smtp_server.envelopes == [] and summary.errors == []


@pytest.mark.browser
def test_a_second_process_leaves_sending_to_the_one_at_work(
    session: Session, user: User, user_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, smtp_server: CapturedMail,
) -> None:  # fmt: skip
    application = _approved(session, settings, user_config, user, lazy)
    summary = RunSummary()
    with sender_lock(settings.data_dir) as mine:
        assert mine
        send_approved(
            session, settings, user_config, summary,
            transport=make_transport(settings), browser=lazy, client=None, now=NOW,
        )  # fmt: skip
        assert smtp_server.envelopes == [] and application.status == "approved"
    send_approved(
        session, settings, user_config, summary,
        transport=make_transport(settings), browser=lazy, client=None, now=NOW,
    )  # fmt: skip
    assert len(smtp_server.envelopes) == 1 and application.status == "submitted"


@pytest.mark.browser
def test_a_crashed_preparation_is_parked_not_retried_every_pass(
    session: Session, user: User, user_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    job = vendor_job(session, user)
    session.commit()
    calls = []

    def broken(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "compose", broken)
    for _ in range(3):
        prepare_pending(
            session, settings, user_config, RunSummary(), browser=lazy, client=None, now=NOW
        )
    application = session.scalar(select(Application).where(Application.job_id == job.id))
    assert len(calls) == 1
    assert application.status == "failed" and "boom" in application.error


# --------------------------------------------------------------------- caps


@pytest.mark.browser
def test_unconfirmed_sends_use_up_the_unattended_allowance(
    session: Session, user: User, auto_config: UserConfig, settings: Settings, lazy: LazyBrowser,
    form_server: FormServer, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr("jobportal.apply.forms.filler.OUTCOME_TIMEOUT_S", 2.0)
    auto_config.search.policy.auto.daily_cap = 1
    auto_config.search.policy.auto.per_company_per_week = 1
    source = make_source(session, "greenhouse", "acme")
    titles = ["Principal Platform Engineer", "Staff Platform Engineer", "Platform Architect"]
    for index, title in enumerate(titles):
        make_job(
            session, source, user=user, title=title, score=95.0 - index,
            apply_url=form_server.url("silent.html"),
        )  # fmt: skip
    session.commit()

    for _ in range(3):  # three worker passes
        summary = RunSummary()
        prepare_pending(session, settings, auto_config, summary, browser=lazy, client=None, now=NOW)
        send_approved(
            session, settings, auto_config, summary,
            transport=None, browser=lazy, client=None, now=NOW,
        )  # fmt: skip

    statuses = sorted(a.status for a in session.scalars(select(Application)))
    assert statuses == ["needs_review", "needs_review", "unconfirmed"]
    assert len(form_server.posts()) == 1  # one attempt, not one per posting


# ----------------------------------------------------------- mail outcomes


class PickyMail(CapturedMail):
    """A mail server that turns some recipients away."""

    def __init__(self, refuse: set[str]) -> None:
        super().__init__()
        self.refuse = refuse

    async def handle_RCPT(
        self, _server: Any, _session: Any, envelope: Any, address: str, _options: Any
    ) -> str:
        if address in self.refuse:
            return "550 5.1.1 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"


@pytest.fixture
def picky(settings: Settings) -> Iterator[PickyMail]:
    import socket

    from aiosmtpd.controller import Controller

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    handler = PickyMail(set())
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    settings.smtp_host, settings.smtp_port, settings.smtp_security = "127.0.0.1", port, "none"
    try:
        yield handler
    finally:
        controller.stop()


@pytest.mark.browser
def test_a_refused_copy_is_not_a_failed_send(
    session: Session, user: User, user_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, picky: PickyMail,
) -> None:  # fmt: skip
    picky.refuse = {"alex@example.com"}  # only your own blind copy
    application = _approved(session, settings, user_config, user, lazy)
    result = submit_application(
        session, settings, user_config, application,
        transport=make_transport(settings), browser=lazy, now=NOW,
    )  # fmt: skip
    assert result == "submitted"
    (envelope,) = picky.envelopes
    assert envelope.rcpt_tos == ["sai@odyssey.example"]
    assert "refused the copy to alex@example.com" in application.confirmation


@pytest.mark.browser
def test_a_refused_addressee_sends_nothing_at_all(
    session: Session, user: User, user_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, picky: PickyMail,
) -> None:  # fmt: skip
    picky.refuse = {"sai@odyssey.example"}
    application = _approved(session, settings, user_config, user, lazy)
    result = submit_application(
        session, settings, user_config, application,
        transport=make_transport(settings), browser=lazy, now=NOW,
    )  # fmt: skip
    assert result == "failed" and "refused the recipient" in application.error
    assert picky.envelopes == []  # not even the copy to yourself
    assert session.scalar(select(LedgerEntry)) is None


class DroppingServer:
    """An SMTP session that accepts everything and dies while the body is sent."""

    def has_extn(self, _name: str) -> bool:
        return False

    def mail(self, _sender: str, _options: list[str]) -> tuple[int, bytes]:
        return 250, b"OK"

    def rcpt(self, _recipient: str) -> tuple[int, bytes]:
        return 250, b"OK"

    def data(self, _payload: bytes) -> tuple[int, bytes]:
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


def test_a_connection_lost_after_the_body_is_unknown_not_refused() -> None:
    message = EmailMessage()
    message["From"] = "Alex <alex@example.com>"
    message["To"] = "sai@odyssey.example"
    message["Subject"] = "Re: role"
    message.set_content("Hello")
    with pytest.raises(MailOutcomeUnknown) as caught:
        _transmit(DroppingServer(), message, ["sai@odyssey.example"])  # type: ignore[arg-type]
    assert isinstance(caught.value, MailError) and "may have been sent" in str(caught.value)


class LostTransport:
    def send(self, message: EmailMessage, recipients: list[str]) -> list[str]:
        raise MailOutcomeUnknown("the connection dropped while the message was being handed over")


@pytest.mark.browser
def test_an_email_of_unknown_fate_is_unconfirmed_and_counted(
    session: Session, user: User, user_config: UserConfig, settings: Settings, lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:  # fmt: skip
    application = _approved(session, settings, user_config, user, lazy)
    result = submit_application(
        session, settings, user_config, application,
        transport=LostTransport(), browser=lazy, now=NOW,
    )  # fmt: skip
    assert result == "unconfirmed" and application.submitted_at == NOW
    assert "Check your sent mail" in application.error
    assert session.scalar(select(OutboundEmail)).status == "unknown"
    assert session.scalar(select(LedgerEntry)).notes == ledger.UNCONFIRMED_NOTE
    # It uses up the email allowance like a confirmed send.
    user_config.search.policy.email.daily_cap = 1
    assert email_send_allowed(session, user_config.search.policy, now=NOW)[0] is False

    # You checked, and it had not arrived: prepare again really sends, once.
    retry(session, application)
    assert session.scalar(select(LedgerEntry)) is None and application.submitted_at is None
    user_config.search.policy.email.daily_cap = 20
    user_config.search.policy.email.min_seconds_between_sends = 0
    prepare_application(
        session, settings, user_config, user, application.job, browser=lazy, now=NOW
    )
    approve(application, now=NOW)
    assert (
        submit_application(
            session, settings, user_config, application,
            transport=make_transport(settings), browser=lazy, now=NOW,
        )
        == "submitted"
    )  # fmt: skip
    assert len(smtp_server.envelopes) == 1


@pytest.mark.browser
def test_sending_again_after_a_bounce_really_sends(
    session: Session, user: User, user_config: UserConfig, settings: Settings,
    lazy: LazyBrowser, smtp_server: CapturedMail,
) -> None:  # fmt: skip
    user_config.search.policy.email.min_seconds_between_sends = 0
    application = _approved(session, settings, user_config, user, lazy)
    transport = make_transport(settings)
    submit_application(
        session, settings, user_config, application, transport=transport, browser=lazy, now=NOW
    )
    sent = session.scalar(select(OutboundEmail))
    bounce = _report("failed", "Undeliverable: Re: role", sent.message_id)
    process_message(session, user_config, user, parse_message(bounce), InboxStats(), NOW)
    assert application.status == "failed"

    request_application(session, user, application.job)
    prepare_application(
        session, settings, user_config, user, application.job, browser=lazy, now=NOW
    )
    approve(application, now=NOW)
    result = submit_application(
        session, settings, user_config, application, transport=transport, browser=lazy, now=NOW
    )
    assert result == "submitted" and len(smtp_server.envelopes) == 2  # no phantom "sent"


# -------------------------------------------------------------------- forms


@pytest.fixture
def book(user_config: UserConfig, tmp_path: Path) -> AnswerBook:
    resume = tmp_path / "Alex_Example_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test resume")
    return AnswerBook(user_config.profile, {}, resume)


@pytest.mark.browser
def test_a_form_with_traps_is_read_as_a_person_would_see_it(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    outcome = filler.prepare(browser, form_server.url("mislabel.html"), book, settings=settings)
    assert outcome.plan is not None
    shown = outcome.plan.to_prepared()
    planned = {item["label"]: item["value"] for item in shown["fields"]}
    names = {p.field.name for p in outcome.plan.fill}

    # The sponsorship question has its own wording, not its neighbour's.
    assert planned["Are you legally authorized to work in the United States?"] == "Yes"
    assert (
        planned[
            "Will you now or in the future require visa sponsorship to work in the United States?"
        ]
        == "No"
    )
    # Fields a person cannot see are not there at all.
    scanned = {f.name for f in [*outcome.plan.unanswered, *outcome.plan.left_blank]} | names
    assert not scanned & {"company_hp", "email_confirm_hp", "hp_gender"}
    # Somebody else's email, a link, a narrow question: none answered from the profile.
    assert not names & {"referrer_email", "resume_link", "sap_years"}
    assert "resume" in names  # the upload itself is
    # A required question the site pre-answered is asked, with the site's choice shown.
    waiting = {q["label"]: q["current"] for q in shown["unanswered"]}
    covenant = (
        "Can you confirm you have no restrictive covenant (non-compete) with a current employer?"
    )
    assert waiting[covenant] == "I confirm"
    assert outcome.status == "needs_answers"
    # An optional box the site ticked is shown as left the way the site set it.
    assert shown["kept"] == [
        {"label": "Share my profile with partner companies and recruiters", "value": "Yes"}
    ]


@pytest.mark.browser
def test_only_the_application_forms_own_submit_button_is_pressed(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    outcome = filler.submit(browser, form_server.url("buttons.html"), book, settings=settings)
    assert outcome.status == "submitted"
    assert outcome.plan is not None and outcome.plan.submit_text == "Submit application"
    assert [post.path for post in form_server.posts()] == ["/submit"]  # no job-alert sign-up
    assert {p.field.name for p in outcome.plan.fill} == {"name"}  # the alert box is not ours


@pytest.mark.browser
@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ("confirm_captcha.html", "failed"),  # "Thank you! One more step: complete the CAPTCHA"
        ("confirm_error.html", "unconfirmed"),  # an error page under "Thank you for your interest"
        ("confirm_redirect.html", "unconfirmed"),  # lands on "create an account"
    ],
)
def test_weak_evidence_is_never_a_confirmation(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings,
    monkeypatch: pytest.MonkeyPatch, page: str, expected: str,
) -> None:  # fmt: skip
    monkeypatch.setattr(filler, "OUTCOME_TIMEOUT_S", 2.0)
    outcome = filler.submit(browser, form_server.url(page), book, settings=settings)
    assert outcome.status == expected and outcome.confirmation == ""


@pytest.mark.browser
def test_an_approval_is_for_the_form_as_it_was_shown(
    session: Session, user: User, user_config: UserConfig, settings: Settings, lazy: LazyBrowser,
    form_server: FormServer,
) -> None:  # fmt: skip
    source = make_source(session, "greenhouse", "acme")
    job = make_job(session, source, user=user, apply_url=form_server.url("classic.html"))
    application = prepare_application(
        session, settings, user_config, user, job, browser=lazy, now=NOW
    )
    assert application.status == "needs_review" and application.prepared["plan_hash"]
    approve(application, now=NOW)

    user_config.profile.phone = "+1 512 555 9999"  # what would be typed is no longer what was shown
    result = submit_application(
        session, settings, user_config, application, transport=None, browser=lazy, now=NOW
    )
    assert result == "needs_review" and application.auto is False
    assert application.blockers[0]["kind"] == "form_changed"
    assert form_server.posts() == []
    # The new plan is what is shown now, and approving that sends it.
    assert any(f["value"] == "+1 512 555 9999" for f in application.prepared["fields"])
    approve(application, now=NOW)
    assert (
        submit_application(
            session, settings, user_config, application, transport=None, browser=lazy, now=NOW
        )
        == "submitted"
    )
    assert application.prepared["url"] == job.apply_url  # never the page it ended on


@pytest.mark.browser
def test_a_page_that_leads_elsewhere_is_not_read(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    target = f"http://localhost:{form_server.port}/classic.html"  # another host name
    url = form_server.url(f"elsewhere.html?to={target}")
    outcome = filler.submit(
        browser, url, book, settings=settings, allowed_hosts=frozenset({"127.0.0.1"})
    )
    assert outcome.status == "needs_human" and outcome.blockers[0]["kind"] == "redirected"
    assert "localhost" in outcome.blockers[0]["detail"] and form_server.posts() == []


def test_addresses_that_parsers_and_browsers_read_differently_are_refused(
    session: Session, user: User, settings: Settings
) -> None:
    source = make_source(session, "lever", "acme")
    for bad in (
        "https://evil.test\\@jobs.lever.co/acme/123/apply",
        "https://user:secret@jobs.lever.co/acme/123/apply",
        "https://evil.test/jobs.lever.co/acme/123/apply",
        "http://[::1",
    ):
        job = make_job(session, source, user=user, apply_url=bad)
        assert form_hosts(job, settings) == frozenset(), bad
        assert service.choose_channel(job, settings).value == "manual"
    with pytest.raises(filler.FormUrlRefused):
        filler.check_form_url("https://evil.test\\@jobs.lever.co/acme/123/apply", settings)
    good = make_job(session, source, user=user, apply_url="https://jobs.lever.co/acme/123/apply")
    assert "jobs.lever.co" in form_hosts(good, settings)


# ------------------------------------------------------------ removed boards


def test_removing_a_board_keeps_what_you_applied_to(session: Session, user: User) -> None:
    from jobportal.crawl import add_source, remove_source, special_source
    from jobportal.models import Source
    from jobportal.sources import SourceSpec

    source = make_source(session, "greenhouse", "acme", "Acme Robotics")
    applied = make_job(session, source, user=user, title="Applied role", external_id="1")
    other = make_job(session, source, user=user, title="Other role", external_id="2")
    application = Application(
        user_id=user.id, job_id=applied.id, channel="form", status="interviewing"
    )
    session.add(application)
    session.flush()
    applied_id, other_id, source_id = applied.id, other.id, source.id

    assert remove_source(session, source, now=NOW) == (1, 1)
    source = session.get(Source, source_id)
    assert source.removed_at == NOW and source.enabled is False
    assert session.get(Job, other_id) is None
    assert session.get(Job, applied_id) is not None
    assert session.get(Application, application.id).status == "interviewing"

    # Adding the board again reconnects to the kept posting: no second copy,
    # so no second application for the same role.
    spec = SourceSpec(kind="greenhouse", token="acme", company_name="Acme Robotics")
    again, created = add_source(session, spec)
    assert created and again.id == source_id and again.removed_at is None and again.enabled

    empty = make_source(session, "lever", "globex")
    make_job(session, empty, user=user, title="Unapplied", external_id="9")
    empty_id = empty.id
    assert remove_source(session, empty) == (1, 0)
    assert session.get(Source, empty_id) is None
    with pytest.raises(ValueError):
        remove_source(session, special_source(session, "manual"))


@pytest.mark.parametrize(
    ("one", "other", "same"),
    [
        ("JP Morgan", "JPMorgan Chase", True),
        ("Southwind Air", "Southwind Air Inc.", True),
        ("Southwind", "Southwind Air", True),
        ("Acme", "Acme Robotics", False),  # too short to tell
        ("Northwind", "Southwind", False),
        ("", "Southwind", False),
    ],
)
def test_the_ledger_recognises_a_client_written_differently(
    one: str, other: str, same: bool
) -> None:
    from jobportal.text import company_key

    assert ledger.same_company(company_key(one), company_key(other)) is same
