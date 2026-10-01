from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import Browser
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.apply import ledger
from jobportal.apply.email_apply import compose
from jobportal.apply.mail import (
    MailError,
    OutgoingMail,
    SmtpTransport,
    build_message,
    save_draft,
)
from jobportal.apply.policy import auto_decision, email_send_allowed
from jobportal.apply.service import (
    ApplicationError,
    approve,
    choose_channel,
    dismiss,
    edit_draft,
    mark_submitted,
    prepare_application,
    provide_answers,
    recover_interrupted,
    request_application,
    set_stage,
    submit_application,
)
from jobportal.browser import LazyBrowser
from jobportal.config import UserConfig
from jobportal.models import (
    Application,
    Job,
    JobScore,
    LedgerEntry,
    OutboundEmail,
    ResumeVariant,
    Source,
    User,
)
from jobportal.pipeline import make_transport
from jobportal.settings import Settings
from jobportal.text import company_key, job_fingerprint
from tests.conftest import NOW, CapturedMail
from tests.formserver import FormServer

DESCRIPTION = (
    "Own the architecture of our Kubernetes platform on AWS with Terraform and GitOps (ArgoCD). "
    "Zero trust identity with Vault, operators in Go, Kafka."
)


# ------------------------------------------------------------------ helpers


def make_source(session: Session, kind: str, token: str = "", company: str = "") -> Source:
    source = Source(kind=kind, token=token or kind, company_name=company, initialized=True)
    session.add(source)
    session.flush()
    return source


def make_job(
    session: Session,
    source: Source,
    *,
    title: str = "Principal Platform Engineer",
    company: str = "Acme Robotics",
    external_id: str = "",
    score: float | None = 92.0,
    lane: str = "career",
    user: User | None = None,
    **fields,
) -> Job:
    count = session.scalar(select(Job.id).order_by(Job.id.desc()).limit(1)) or 0
    values = {
        "location": "Remote - US",
        "remote": True,
        "employment_type": "full_time",
        "description_text": DESCRIPTION,
        "url": "https://example.com/job",
        "posted_at": NOW - timedelta(hours=5),
        "first_seen_at": NOW - timedelta(hours=4),
        "last_seen_at": NOW,
    }
    values.update(fields)
    job = Job(
        source_id=source.id,
        external_id=external_id or f"job-{count + 1}",
        company_name=company,
        company_key=company_key(company),
        title=title,
        fingerprint=job_fingerprint(company, title),
        **values,
    )
    session.add(job)
    session.flush()
    if score is not None and user is not None:
        session.add(
            JobScore(
                job_id=job.id,
                user_id=user.id,
                lane=lane,
                score=score,
                decision="shortlist",
                scored_at=NOW,
            )
        )
        session.flush()
    return job


def vendor_job(
    session: Session,
    user: User,
    *,
    vendor: str = "Odyssey Staffing",
    client: str = "Southwind Air",
    **fields,
) -> Job:
    """A contract requirement that arrived by email from a staffing vendor."""
    source = session.scalar(select(Source).where(Source.kind == "email")) or make_source(
        session, "email"
    )
    values = {
        "title": "Cloud Architect",
        "company": vendor,
        "client_name": client,
        "contact_name": "Sai Kumar",
        "contact_email": "sai@odyssey.example",
        "employment_type": "contract",
        "lane": "contract",
        "raw": {
            "subject": "Urgent requirement: Cloud Architect (Remote)",
            "message_id": "<req-1@odyssey.example>",
        },
    }
    values.update(fields)
    return make_job(session, source, user=user, **values)


@pytest.fixture
def lazy(settings: Settings, browser: Browser) -> LazyBrowser:
    return LazyBrowser(settings, browser=browser)


@pytest.fixture
def auto_config(user_config: UserConfig) -> UserConfig:
    config = user_config.model_copy(deep=True)
    config.search.policy.mode = "auto"
    config.search.policy.email.min_seconds_between_sends = 0
    return config


def events(application: Application) -> list[str]:
    return [event.kind for event in application.events]


# --------------------------------------------------------------------- mail


def _mail(tmp_path: Path, **changes) -> OutgoingMail:
    attachment = tmp_path / "Alex_Example_Resume.pdf"
    attachment.write_bytes(b"%PDF-1.4 resume")
    values = {
        "sender": "alex@example.com",
        "sender_name": "Alex Example",
        "to": "sai@odyssey.example",
        "subject": "Re: Cloud Architect",
        "body": "Hello,\n\nPlease find my resume attached.\n",
        "attachments": [attachment],
        "bcc": ["alex@example.com"],
    }
    values.update(changes)
    return OutgoingMail(**values)


def test_build_message_has_headers_body_and_attachment(tmp_path: Path) -> None:
    mail = _mail(tmp_path, in_reply_to="<req-1@odyssey.example>")
    message = build_message(mail)
    assert message["From"] == "Alex Example <alex@example.com>"
    assert message["To"] == "sai@odyssey.example"
    assert message["In-Reply-To"] == "<req-1@odyssey.example>"
    assert message["Message-ID"].endswith("@example.com>")
    assert "Bcc" not in message  # blind copies travel in the envelope only
    assert mail.recipients() == ["sai@odyssey.example", "alex@example.com"]
    (attachment,) = list(message.iter_attachments())
    assert attachment.get_filename() == "Alex_Example_Resume.pdf"
    assert attachment.get_content_type() == "application/pdf"


@pytest.mark.parametrize(
    "changes",
    [
        {"to": "not-an-address"},
        {"to": "a@example.com, b@example.com"},
        {"to": "a@example.com\nBcc: evil@example.com"},
        {"subject": "   "},
        {"body": "  \n "},
        {"bcc": ["nope"]},
    ],
)
def test_build_message_rejects_bad_input(tmp_path: Path, changes: dict) -> None:
    with pytest.raises(MailError):
        build_message(_mail(tmp_path, **changes))


def test_subject_line_breaks_cannot_inject_headers(tmp_path: Path) -> None:
    message = build_message(_mail(tmp_path, subject="Cloud Architect\r\nBcc: evil@example.com"))
    assert message["Subject"] == "Cloud Architect Bcc: evil@example.com"
    assert "Bcc" not in message


def test_missing_attachment_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MailError, match="attachment is missing"):
        build_message(_mail(tmp_path, attachments=[tmp_path / "gone.pdf"]))


def test_smtp_transport_delivers_to_all_recipients(
    tmp_path: Path, settings: Settings, smtp_server: CapturedMail
) -> None:
    mail = _mail(tmp_path)
    SmtpTransport(settings).send(build_message(mail), mail.recipients())
    (envelope,) = smtp_server.envelopes
    assert envelope.mail_from == "alex@example.com"
    assert envelope.rcpt_tos == ["sai@odyssey.example", "alex@example.com"]
    (received,) = smtp_server.messages()
    assert received["Subject"] == "Re: Cloud Architect"


def test_smtp_errors_become_mail_errors(
    tmp_path: Path, settings: Settings, smtp_server: CapturedMail
) -> None:
    smtp_server.fail_with = "550 Mailbox unavailable"
    mail = _mail(tmp_path)
    with pytest.raises(MailError, match="refused"):
        SmtpTransport(settings).send(build_message(mail), mail.recipients())
    settings.smtp_port = 1  # nothing listens here
    with pytest.raises(MailError, match="could not reach"):
        SmtpTransport(settings).send(build_message(mail), mail.recipients())


def test_password_is_never_sent_without_tls_to_a_remote_server(
    tmp_path: Path, settings: Settings, monkeypatch
) -> None:
    import smtplib

    class FakeSMTP:
        def __init__(self, *_a, **_k) -> None: ...
        def __enter__(self):
            return self

        def __exit__(self, *_a) -> None: ...
        def login(self, *_a) -> None:
            raise AssertionError("login must not be attempted")

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    settings.smtp_host, settings.smtp_security = "mail.example.com", "none"
    settings.smtp_username = "alex"
    mail = _mail(tmp_path)
    with pytest.raises(MailError, match="without TLS"):
        SmtpTransport(settings).send(build_message(mail), mail.recipients())


def test_save_draft_opens_as_unsent(tmp_path: Path) -> None:
    path = save_draft(build_message(_mail(tmp_path)), tmp_path / "outbox", "application-7")
    content = path.read_bytes()
    assert path.name == "application-7.eml" and content.startswith(b"X-Unsent: 1\n")
    assert b"Subject: Re: Cloud Architect" in content


# ------------------------------------------------------------------ compose


def _variant(
    session: Session, user: User, job: Job, tmp_path: Path, matched: list[str]
) -> ResumeVariant:
    pdf = tmp_path / "Alex_Example_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    variant = ResumeVariant(
        user_id=user.id,
        job_id=job.id,
        variant="contract",
        pdf_path=str(pdf),
        docx_path=str(tmp_path / "Alex_Example_Resume.docx"),
        content={
            "headline": "Cloud and Kubernetes Architect",
            "skills": [{"group": "Cloud", "items": ["Kubernetes", "AWS", "Terraform", "Python"]}],
        },
        matched=matched,
    )
    session.add(variant)
    session.flush()
    return variant


def test_compose_reply_to_a_vendor(
    session: Session, user: User, user_config: UserConfig, tmp_path: Path
) -> None:
    job = vendor_job(session, user)
    variant = _variant(
        session, user, job, tmp_path, ["AWS", "Terraform", "migration", "Kubernetes"]
    )
    draft = compose(job, user_config.profile, variant)

    assert draft.to == "sai@odyssey.example"
    assert draft.subject == "Re: Urgent requirement: Cloud Architect (Remote)"
    assert draft.in_reply_to == "<req-1@odyssey.example>"
    assert draft.attachments == [variant.pdf_path]
    assert draft.body.startswith(
        "Hi Sai,\n\nI am interested in the Cloud Architect role with Southwind Air."
    )
    assert "I am a Cloud and Kubernetes Architect with 15 years of experience." in draft.body
    # skills come from the resume's skill groups only; "migration" is a tag, not a skill
    assert "my strongest areas are AWS, Terraform, Kubernetes." in draft.body
    assert "Availability: 2 weeks from offer" in draft.body
    assert "Engagement: C2C or W2" in draft.body
    assert "authorized to work in the United States; no sponsorship needed" in draft.body
    assert "Rate:" not in draft.body  # no rate configured, none quoted
    assert draft.body.rstrip().endswith("https://www.linkedin.com/in/alex-example")


def test_compose_states_only_what_the_profile_says(
    session: Session, user: User, user_config: UserConfig, tmp_path: Path
) -> None:
    profile = user_config.profile.model_copy(deep=True)
    profile.work_authorization.needs_sponsorship = None
    profile.contract.rate = "$120/hr on C2C"
    profile.years_experience = None
    job = vendor_job(session, user, contact_name="recruiting-team", raw={})
    variant = _variant(session, user, job, tmp_path, [])
    draft = compose(job, profile, variant, attach_docx=True)

    assert draft.subject == "Cloud Architect - Alex Example"
    assert draft.body.startswith("Hello,")  # not a personal name: no "Hi recruiting-team"
    assert "Work authorization" not in draft.body
    assert "years of experience" not in draft.body and "strongest areas" not in draft.body
    assert "Rate: $120/hr on C2C" in draft.body
    assert len(draft.attachments) == 2


def test_compose_direct_application_has_no_vendor_logistics(
    session: Session, user: User, user_config: UserConfig, tmp_path: Path
) -> None:
    source = make_source(session, "manual")
    job = make_job(
        session, source, user=user, contact_email="jobs@acme.example", requisition_id="REQ-9"
    )
    draft = compose(
        job, user_config.profile, _variant(session, user, job, tmp_path, ["Kubernetes"])
    )
    assert draft.subject == "Principal Platform Engineer (REQ-9) - Alex Example"
    assert "Availability:" not in draft.body and "Engagement:" not in draft.body


# ------------------------------------------------------------------- ledger


def _sent_application(session: Session, user: User, job: Job, when=NOW) -> Application:
    application = Application(
        user_id=user.id, job_id=job.id, channel="email", status="submitted", submitted_at=when
    )
    session.add(application)
    session.flush()
    ledger.record(session, application, now=when)
    return application


def test_ledger_parties(session: Session, user: User) -> None:
    direct = make_job(session, make_source(session, "greenhouse", "acme"), user=user)
    assert ledger.parties(direct) == ("Acme Robotics", "")
    assert ledger.parties(vendor_job(session, user)) == ("Southwind Air", "Odyssey Staffing")
    manual = make_job(
        session, make_source(session, "manual"), user=user, title="X", company="TekVendor",
        client_name="Big Bank", raw={"vendor": True},
    )  # fmt: skip
    assert ledger.parties(manual) == ("Big Bank", "TekVendor")


def test_ledger_blocks_a_second_route_to_the_same_client(session: Session, user: User) -> None:
    first = vendor_job(session, user)
    _sent_application(session, user, first)

    def conflict(client: str, vendor: str, days: int = 180, now=NOW + timedelta(days=3)):
        return ledger.find_conflict(
            session, user.id, client=client, vendor=vendor, window_days=days, now=now
        )

    other_vendor = conflict("Southwind Air, Inc.", "Insight Global")
    assert other_vendor is not None
    assert ledger.describe(other_vendor) == (
        "You were already submitted to Southwind Air through Odyssey Staffing on 30 Sep 2026 for 'Cloud Architect'."
    )
    assert conflict("Southwind Air", "") is not None  # applying directly is a second route too
    assert conflict("Southwind Air", "Odyssey Staffing") is None  # same vendor, another role
    assert conflict("Northeast Rail", "Insight Global") is None
    assert conflict("", "Insight Global") is None  # unknown client cannot be checked here
    assert (
        conflict("Southwind Air", "Insight Global", now=NOW + timedelta(days=200)) is None
    )  # window passed


# ------------------------------------------------------------------- policy


def test_auto_decision_rules(session: Session, user: User, auto_config: UserConfig) -> None:
    policy = auto_config.search.policy
    source = make_source(session, "greenhouse", "acme")
    job = make_job(session, source, user=user, score=92.0)
    score = session.scalar(select(JobScore).where(JobScore.job_id == job.id))

    assert auto_decision(session, user.id, policy, job, score, "form", now=NOW).allowed

    review = policy.model_copy(update={"mode": "review"})
    assert auto_decision(session, user.id, review, job, score, "form", now=NOW).reasons == [
        "Review mode: every application waits for your approval"
    ]
    score.score = 70.0
    assert (
        "below your unattended threshold of 80"
        in auto_decision(session, user.id, policy, job, score, "form", now=NOW).reasons[0]
    )
    score.score = 92.0
    assert auto_decision(session, user.id, policy, job, None, "form", now=NOW).reasons == [
        "Not on the shortlist"
    ]

    email_only = policy.model_copy(deep=True)
    email_only.auto.channels = ["email"]
    assert (
        "not enabled for form"
        in auto_decision(session, user.id, email_only, job, score, "form", now=NOW).reasons[0]
    )

    old = make_job(session, source, user=user, title="Old Role", posted_at=NOW - timedelta(days=45))
    old_score = session.scalar(select(JobScore).where(JobScore.job_id == old.id))
    assert auto_decision(session, user.id, policy, old, old_score, "form", now=NOW).reasons == [
        "Posted more than 30 days ago"
    ]
    backlog = make_job(
        session, source, user=user, title="Backlog Role", posted_at=None, is_backfill=True
    )
    backlog_score = session.scalar(select(JobScore).where(JobScore.job_id == backlog.id))
    assert auto_decision(
        session, user.id, policy, backlog, backlog_score, "form", now=NOW
    ).reasons == ["The posting's age is unknown"]


def test_auto_caps_count_what_went_out_and_what_is_queued(
    session: Session, user: User, auto_config: UserConfig
) -> None:
    policy = auto_config.search.policy.model_copy(deep=True)
    policy.auto.daily_cap = 2
    policy.auto.per_company_per_week = 2
    acme = make_source(session, "greenhouse", "acme")
    globex = make_source(session, "lever", "globex")
    jobs = [make_job(session, acme, user=user, title=f"Role {i}") for i in range(3)]
    other = make_job(session, globex, user=user, company="Globex", title="Other")
    score = session.scalar(select(JobScore).where(JobScore.job_id == other.id))

    session.add(
        Application(
            user_id=user.id,
            job_id=jobs[0].id,
            channel="form",
            status="submitted",
            auto=True,
            submitted_at=NOW - timedelta(hours=2),
        )
    )
    session.add(
        Application(
            user_id=user.id, job_id=jobs[1].id, channel="form", status="approved", auto=True
        )
    )
    session.flush()

    blocked = auto_decision(session, user.id, policy, other, score, "form", now=NOW)
    assert blocked.reasons == ["Daily cap of 2 unattended applications reached"]

    third = auto_decision(
        session,
        user.id,
        policy.model_copy(update={"auto": policy.auto.model_copy(update={"daily_cap": 10})}),
        jobs[2],
        score,
        "form",
        now=NOW,
    )
    assert third.reasons == ["Already 2 applications to Acme Robotics this week (limit 2)"]
    # A manual send from yesterday does not use up the unattended allowance.
    session.add(
        Application(
            user_id=user.id,
            job_id=other.id,
            channel="form",
            status="submitted",
            auto=False,
            submitted_at=NOW - timedelta(hours=1),
        )
    )
    session.flush()
    assert (
        "Daily cap"
        in auto_decision(session, user.id, policy, jobs[2], score, "form", now=NOW).reasons[0]
    )


def test_email_throttle(session: Session, user_config: UserConfig) -> None:
    policy = user_config.search.policy.model_copy(deep=True)
    policy.email.daily_cap = 2
    policy.email.min_seconds_between_sends = 60
    assert email_send_allowed(session, policy, now=NOW) == (True, "")
    session.add(
        OutboundEmail(
            to_addr="a@x.example",
            subject="s",
            body="b",
            status="sent",
            sent_at=NOW - timedelta(seconds=20),
        )
    )
    session.flush()
    assert email_send_allowed(session, policy, now=NOW) == (False, "Next email can go out in 41s")
    assert email_send_allowed(session, policy, now=NOW + timedelta(seconds=50))[0]
    session.add(
        OutboundEmail(
            to_addr="b@x.example",
            subject="s",
            body="b",
            status="sent",
            sent_at=NOW - timedelta(hours=3),
        )
    )
    session.add(OutboundEmail(to_addr="c@x.example", subject="s", body="b", status="failed"))
    session.flush()
    assert email_send_allowed(session, policy, now=NOW + timedelta(minutes=5)) == (
        False,
        "Daily cap of 2 emails reached",
    )


# ------------------------------------------------------------ service: email


@pytest.mark.browser
def test_email_application_review_approve_send(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:
    job = vendor_job(session, user)
    application = prepare_application(
        session, settings, user_config, user, job, browser=lazy, now=NOW
    )

    assert application.status == "needs_review" and application.auto is False
    assert application.channel == "email" and application.blockers == []
    assert application.prepared["to"] == "sai@odyssey.example"
    assert application.prepared["auto_notes"] == [
        "Review mode: every application waits for your approval"
    ]
    assert smtp_server.envelopes == []  # preparing never sends

    transport = make_transport(settings)
    with pytest.raises(ApplicationError):
        submit_application(
            session, settings, user_config, application, transport=transport, browser=lazy, now=NOW
        )

    approve(application, now=NOW)
    result = submit_application(
        session, settings, user_config, application, transport=transport, browser=lazy, now=NOW
    )

    assert result == "submitted" and application.status == "submitted"
    (envelope,) = smtp_server.envelopes
    assert envelope.rcpt_tos == ["sai@odyssey.example", "alex@example.com"]  # bcc to self
    (message,) = smtp_server.messages()
    assert message["Subject"] == "Re: Urgent requirement: Cloud Architect (Remote)"
    assert message["In-Reply-To"] == "<req-1@odyssey.example>"
    (attachment,) = list(message.iter_attachments())
    assert attachment.get_filename() == "Alex_Example_Resume.pdf"
    assert attachment.get_content().startswith(b"%PDF")

    assert application.submitted_at == NOW
    assert application.follow_up_at == NOW + timedelta(days=7)
    assert "Sent to sai@odyssey.example" in application.confirmation
    entry = session.scalar(select(LedgerEntry))
    assert (entry.client_name, entry.vendor_name, entry.engagement) == (
        "Southwind Air",
        "Odyssey Staffing",
        "c2c/w2",
    )
    outbound = session.scalar(select(OutboundEmail))
    assert outbound.status == "sent" and outbound.message_id == message["Message-ID"]
    assert events(application) == ["prepared", "approved", "submitted"]

    # Approving or sending again is refused: one application, one email.
    with pytest.raises(ApplicationError):
        approve(application)
    with pytest.raises(ApplicationError):
        submit_application(
            session, settings, user_config, application, transport=transport, browser=lazy, now=NOW
        )
    assert len(smtp_server.envelopes) == 1


@pytest.mark.browser
def test_edited_draft_is_what_gets_sent(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:
    application = prepare_application(
        session, settings, user_config, user, vendor_job(session, user), browser=lazy, now=NOW
    )
    edit_draft(
        application,
        subject="Cloud Architect - available now",
        body="Hi Sai,\n\nResume attached.\n\nAlex",
    )
    approve(application, now=NOW)
    submit_application(
        session,
        settings,
        user_config,
        application,
        transport=make_transport(settings),
        browser=lazy,
        now=NOW,
    )
    (message,) = smtp_server.messages()
    assert message["Subject"] == "Cloud Architect - available now"
    body = message.get_body().get_content().replace("\r\n", "\n")  # SMTP carries CRLF
    assert body.strip() == "Hi Sai,\n\nResume attached.\n\nAlex"
    with pytest.raises(ApplicationError):
        edit_draft(application, subject="x", body="y")  # too late once sent


@pytest.mark.browser
def test_auto_mode_sends_unattended_and_ledger_stops_the_second_vendor(
    session: Session,
    user: User,
    auto_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:
    transport = make_transport(settings)
    first = prepare_application(
        session, settings, auto_config, user, vendor_job(session, user), browser=lazy, now=NOW
    )
    assert first.status == "approved" and first.auto is True
    assert (
        submit_application(
            session, settings, auto_config, first, transport=transport, browser=lazy, now=NOW
        )
        == "submitted"
    )

    rival = vendor_job(
        session, user, vendor="Insight Global", title="Cloud Platform Architect",
        contact_email="pat@insight.example", contact_name="Pat", raw={},
    )  # fmt: skip
    second = prepare_application(
        session, settings, auto_config, user, rival, browser=lazy, now=NOW + timedelta(hours=1)
    )
    assert second.status == "needs_review" and second.auto is False
    assert second.blockers[0]["kind"] == "ledger_conflict"
    assert (
        "already submitted to Southwind Air through Odyssey Staffing"
        in second.blockers[0]["detail"]
    )

    unnamed = vendor_job(
        session, user, vendor="TekVendor", client="", title="DevOps Architect",
        contact_email="r@tek.example", raw={},
    )  # fmt: skip
    third = prepare_application(
        session, settings, auto_config, user, unnamed, browser=lazy, now=NOW + timedelta(hours=1)
    )
    assert third.status == "needs_review"
    assert third.blockers[0]["kind"] == "client_unknown"
    assert len(smtp_server.envelopes) == 1  # only the first went out by itself

    approve(second, now=NOW + timedelta(hours=2))  # your call overrides the warning, and is logged
    assert second.events[-1].detail == {"overrode": ["ledger_conflict"]}


@pytest.mark.browser
def test_auto_rules_are_rechecked_at_send_time(
    session: Session,
    user: User,
    auto_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:
    application = prepare_application(
        session, settings, auto_config, user, vendor_job(session, user), browser=lazy, now=NOW
    )
    assert application.status == "approved"
    auto_config.search.policy.auto.min_score = 99  # the bar moved before it went out
    result = submit_application(
        session,
        settings,
        auto_config,
        application,
        transport=make_transport(settings),
        browser=lazy,
        now=NOW,
    )
    assert result == "needs_review" and application.auto is False
    assert smtp_server.envelopes == []
    assert "below your unattended threshold of 99" in application.prepared["auto_notes"][0]


@pytest.mark.browser
def test_without_a_mail_server_a_draft_is_saved_for_you(
    session: Session, user: User, auto_config: UserConfig, settings: Settings, lazy: LazyBrowser
) -> None:
    application = prepare_application(
        session, settings, auto_config, user, vendor_job(session, user), browser=lazy, now=NOW
    )
    assert application.status == "needs_human"
    assert application.blockers[0]["kind"] == "no_smtp"
    draft = Path(application.prepared["draft_file"])
    assert draft.exists() and draft.parent == settings.outbox_dir
    assert b"To: sai@odyssey.example" in draft.read_bytes()

    mark_submitted(session, auto_config, application, now=NOW, note="Sent from my mail client")
    assert application.status == "submitted" and application.auto is False
    assert session.scalar(select(LedgerEntry)).client_name == "Southwind Air"


@pytest.mark.browser
def test_mail_failure_is_not_recorded_as_sent(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:
    application = prepare_application(
        session, settings, user_config, user, vendor_job(session, user), browser=lazy, now=NOW
    )
    approve(application, now=NOW)
    smtp_server.fail_with = "554 Rejected as spam"
    result = submit_application(
        session,
        settings,
        user_config,
        application,
        transport=make_transport(settings),
        browser=lazy,
        now=NOW,
    )
    assert result == "failed" and "refused" in application.error
    assert application.submitted_at is None
    assert session.scalar(select(LedgerEntry)) is None
    assert session.scalar(select(OutboundEmail)).status == "failed"


@pytest.mark.browser
def test_email_throttle_defers_instead_of_sending(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:
    transport = make_transport(settings)
    first = prepare_application(
        session, settings, user_config, user, vendor_job(session, user), browser=lazy, now=NOW
    )
    second_job = vendor_job(
        session,
        user,
        vendor="TekVendor",
        client="Northeast Rail",
        title="AWS Architect",
        contact_email="r@tek.example",
        raw={},
    )
    second = prepare_application(
        session, settings, user_config, user, second_job, browser=lazy, now=NOW
    )
    approve(first, now=NOW)
    approve(second, now=NOW)

    assert (
        submit_application(
            session, settings, user_config, first, transport=transport, browser=lazy, now=NOW
        )
        == "submitted"
    )
    soon = NOW + timedelta(seconds=10)
    assert (
        submit_application(
            session, settings, user_config, second, transport=transport, browser=lazy, now=soon
        )
        == "deferred"
    )
    assert second.status == "approved" and len(smtp_server.envelopes) == 1
    later = NOW + timedelta(seconds=90)
    assert (
        submit_application(
            session, settings, user_config, second, transport=transport, browser=lazy, now=later
        )
        == "submitted"
    )


# ---------------------------------------------------------- service: guards


@pytest.mark.browser
def test_blocked_closed_and_duplicate_roles_are_skipped(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:
    source = make_source(session, "greenhouse", "acme")
    blocked = make_job(
        session, source, user=user, company="Northwind Systems", title="Staff Engineer"
    )
    closed = make_job(session, source, user=user, title="Closed Role", closed_at=NOW)
    blocked_client = vendor_job(session, user, client="Northwind Systems Inc")

    def kind_of(job: Job) -> tuple[str, str]:
        application = prepare_application(
            session, settings, user_config, user, job, browser=lazy, now=NOW
        )
        return application.status, application.blockers[0]["kind"]

    assert kind_of(blocked) == ("skipped", "blocked_company")
    assert kind_of(blocked_client) == ("skipped", "blocked_company")
    assert kind_of(closed) == ("skipped", "closed")

    austin = vendor_job(session, user, location="Austin, TX")
    dallas = vendor_job(session, user, location="Dallas, TX", contact_email="other@odyssey.example")
    first = prepare_application(session, settings, user_config, user, austin, browser=lazy, now=NOW)
    twin = prepare_application(session, settings, user_config, user, dallas, browser=lazy, now=NOW)
    assert first.status == "needs_review"
    assert twin.status == "skipped" and twin.blockers[0]["kind"] == "duplicate"
    assert f"application #{first.id}" in twin.blockers[0]["detail"]


@pytest.mark.browser
def test_job_closed_after_approval_is_not_sent(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    smtp_server: CapturedMail,
) -> None:
    job = vendor_job(session, user)
    application = prepare_application(
        session, settings, user_config, user, job, browser=lazy, now=NOW
    )
    approve(application, now=NOW)
    job.closed_at = NOW
    result = submit_application(
        session,
        settings,
        user_config,
        application,
        transport=make_transport(settings),
        browser=lazy,
        now=NOW,
    )
    assert result == "skipped" and smtp_server.envelopes == []


def test_interrupted_sends_are_surfaced_not_retried(session: Session, user: User) -> None:
    job = vendor_job(session, user)
    stuck = Application(user_id=user.id, job_id=job.id, channel="email", status="submitting")
    session.add(stuck)
    session.flush()
    assert recover_interrupted(session, now=NOW) == 0  # just started: leave it
    stuck.updated_at = NOW - timedelta(hours=1)
    session.flush()
    assert recover_interrupted(session, now=NOW) == 1
    assert stuck.status == "failed" and "not known whether this went out" in stuck.error


def test_dismiss_and_stage_changes(session: Session, user: User, user_config: UserConfig) -> None:
    job = vendor_job(session, user)
    application = Application(
        user_id=user.id, job_id=job.id, channel="email", status="needs_review"
    )
    session.add(application)
    session.flush()
    with pytest.raises(ApplicationError):
        set_stage(application, "interviewing")  # nothing has gone out yet

    mark_submitted(session, user_config, application, now=NOW)
    with pytest.raises(ApplicationError):
        dismiss(application)
    set_stage(application, "replied", now=NOW)
    assert application.next_action == "Reply" and application.follow_up_at is None
    set_stage(application, "interviewing", now=NOW, note="Panel on Friday")
    assert application.notes == "Panel on Friday"
    set_stage(application, "rejected", now=NOW)
    assert application.status == "rejected" and application.next_action == ""
    with pytest.raises(ApplicationError):
        set_stage(application, "needs_review")

    other = Application(
        user_id=user.id,
        job_id=vendor_job(session, user, title="Other").id,
        channel="email",
        status="needs_review",
    )
    session.add(other)
    session.flush()
    dismiss(other, "Rate too low")
    assert other.status == "skipped" and other.blockers[0]["detail"] == "Rate too low"


def test_choose_channel(session: Session, user: User) -> None:
    assert choose_channel(vendor_job(session, user)).value == "email"
    greenhouse = make_source(session, "greenhouse", "acme")
    assert (
        choose_channel(
            make_job(session, greenhouse, user=user, apply_url="https://x.example/apply")
        ).value
        == "form"
    )
    workday = make_source(session, "workday", "x.wd5.myworkdayjobs.com/x/Site")
    assert (
        choose_channel(
            make_job(session, workday, user=user, title="W", apply_url="https://x.example/apply")
        ).value
        == "manual"
    )
    assert (
        choose_channel(make_job(session, greenhouse, user=user, title="No URL")).value == "manual"
    )


# ------------------------------------------------------------- service: form


@pytest.mark.browser
def test_form_application_end_to_end(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    form_server: FormServer,
) -> None:
    source = make_source(session, "greenhouse", "acme")
    job = make_job(session, source, user=user, apply_url=form_server.url("classic.html"))

    application = prepare_application(
        session, settings, user_config, user, job, browser=lazy, now=NOW
    )
    assert application.status == "needs_review" and application.channel == "form"
    fields = {f["label"]: f["value"] for f in application.prepared["fields"]}
    assert fields["First Name"] == "Alex" and fields["Resume/CV"] == "Alex_Example_Resume.pdf"
    assert form_server.posts() == []

    approve(application, now=NOW)
    result = submit_application(
        session, settings, user_config, application, transport=None, browser=lazy, now=NOW
    )
    assert result == "submitted"
    (sent,) = form_server.posts()
    assert sent.first("job_application[email]") == "alex@example.com"
    assert sent.files["job_application[resume]"][0] == "Alex_Example_Resume.pdf"
    assert sent.files["job_application[resume]"][1] > 1000  # the real tailored PDF
    assert "Thank you for applying" in application.confirmation
    shots = application.prepared["screenshots"]
    assert len(shots) == 2 and all(Path(s).parent == settings.screenshots_dir for s in shots)
    entry = session.scalar(select(LedgerEntry))
    assert (entry.client_name, entry.vendor_name, entry.channel) == ("Acme Robotics", "", "form")


@pytest.mark.browser
def test_form_questions_are_asked_once_and_remembered(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    form_server: FormServer,
) -> None:
    source = make_source(session, "greenhouse", "acme")
    job = make_job(session, source, user=user, apply_url=form_server.url("custom_questions.html"))
    application = prepare_application(
        session, settings, user_config, user, job, browser=lazy, now=NOW
    )
    assert application.status == "needs_answers"
    open_questions = application.prepared["unanswered"]
    assert [q["label"] for q in open_questions] == [
        "Why do you want to work at Acme Robotics?",
        "I have read and agree to the candidate privacy policy",
    ]
    with pytest.raises(ApplicationError):
        approve(application)

    saved = provide_answers(
        session,
        application,
        {
            open_questions[0]["key"]: "The platform roadmap.",
            open_questions[1]["key"]: "Yes",
            "made-up-key": "ignored",
        },
    )
    assert saved == 2 and application.status == "preparing"

    again = prepare_application(session, settings, user_config, user, job, browser=lazy, now=NOW)
    assert again.id == application.id and again.status == "needs_review"
    assert again.prepared["unanswered"] == []
    approve(again, now=NOW)
    assert (
        submit_application(
            session, settings, user_config, again, transport=None, browser=lazy, now=NOW
        )
        == "submitted"
    )
    assert form_server.posts()[0].first("job_application[answers][9]") == "The platform roadmap."

    # A second company asking the same question needs nothing from you.
    other = make_job(
        session,
        source,
        user=user,
        title="Staff Platform Engineer",
        apply_url=form_server.url("custom_questions.html"),
    )
    assert (
        prepare_application(
            session, settings, user_config, user, other, browser=lazy, now=NOW
        ).status
        == "needs_review"
    )


@pytest.mark.browser
def test_bot_checked_form_is_handed_to_you_even_in_auto_mode(
    session: Session,
    user: User,
    auto_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    form_server: FormServer,
) -> None:
    source = make_source(session, "lever", "globex", "Globex")
    job = make_job(
        session, source, user=user, company="Globex", apply_url=form_server.url("captcha.html")
    )
    application = prepare_application(
        session, settings, auto_config, user, job, browser=lazy, now=NOW
    )
    assert application.status == "needs_human" and application.auto is False
    assert application.blockers[0]["kind"] == "bot_check"
    assert len(application.prepared["fields"]) > 5  # answers ready for the hand-off
    assert form_server.posts() == []

    mark_submitted(session, auto_config, application, now=NOW, note="Submitted by hand")
    assert application.status == "submitted"
    assert session.scalar(select(LedgerEntry)).client_name == "Globex"


@pytest.mark.browser
def test_auto_mode_submits_a_clean_form_unattended(
    session: Session,
    user: User,
    auto_config: UserConfig,
    settings: Settings,
    lazy: LazyBrowser,
    form_server: FormServer,
) -> None:
    source = make_source(session, "greenhouse", "acme")
    job = make_job(session, source, user=user, apply_url=form_server.url("classic.html"))
    application = prepare_application(
        session, settings, auto_config, user, job, browser=lazy, now=NOW
    )
    assert application.status == "approved" and application.auto is True
    assert (
        submit_application(
            session, settings, auto_config, application, transport=None, browser=lazy, now=NOW
        )
        == "submitted"
    )
    assert len(form_server.posts()) == 1 and application.events[-1].detail["auto"] is True


@pytest.mark.browser
def test_unconfirmed_form_submission_is_failed_and_not_retried(
    session: Session, user: User, user_config: UserConfig, settings: Settings, lazy: LazyBrowser,
    form_server: FormServer, monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr("jobportal.apply.forms.filler.OUTCOME_TIMEOUT_S", 2.0)
    source = make_source(session, "greenhouse", "acme")
    job = make_job(session, source, user=user, apply_url=form_server.url("silent.html"))
    application = prepare_application(
        session, settings, user_config, user, job, browser=lazy, now=NOW
    )
    approve(application, now=NOW)
    assert (
        submit_application(
            session, settings, user_config, application, transport=None, browser=lazy, now=NOW
        )
        == "failed"
    )
    assert "did not confirm" in application.error and application.submitted_at is None
    assert session.scalar(select(LedgerEntry)) is None
    with pytest.raises(ApplicationError):  # a failed application is never sent again by itself
        submit_application(
            session, settings, user_config, application, transport=None, browser=lazy, now=NOW
        )
    assert len(form_server.posts()) == 1


@pytest.mark.browser
def test_request_application_and_manual_channel(
    session: Session, user: User, user_config: UserConfig, settings: Settings, lazy: LazyBrowser
) -> None:
    workday = make_source(session, "workday", "x.wd5.myworkdayjobs.com/x/Site", "Example Corp")
    job = make_job(
        session,
        workday,
        user=user,
        company="Example Corp",
        score=None,
        apply_url="https://x.example/apply",
    )
    requested = request_application(session, user, job)
    assert requested.status == "preparing" and events(requested) == ["requested"]
    assert request_application(session, user, job).id == requested.id

    application = prepare_application(
        session, settings, user_config, user, job, browser=lazy, now=NOW
    )
    assert application.id == requested.id
    assert application.status == "needs_human" and application.blockers[0]["kind"] == "manual"
    assert Path(application.prepared["resume"]).exists()  # the tailored resume is ready to upload
