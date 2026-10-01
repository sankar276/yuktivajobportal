from __future__ import annotations

import imaplib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.apply import ledger
from jobportal.apply.service import choose_channel
from jobportal.comp import Comp
from jobportal.config import UserConfig
from jobportal.inbox.imap import Fetched, InboxError, fetch_new
from jobportal.inbox.ingest import ingest_inbox
from jobportal.inbox.parse import extract_requirement, looks_like_requirement, parse_message
from jobportal.models import Application, InboundEmail, Job, OutboundEmail, Source, User
from jobportal.scoring import score_job
from jobportal.settings import Settings
from tests.conftest import FIXTURES, NOW

OUR_ID = "<sent-1@example.com>"


def mail(name: str) -> bytes:
    text = (FIXTURES / "mail" / name).read_text(encoding="utf-8")
    return text.replace("__OUR_MESSAGE_ID__", OUR_ID).encode("utf-8")


def fetcher(messages: list[tuple[int, bytes]]):
    """A stand-in for the IMAP client that serves the given ``(uid, raw)`` messages."""
    calls: list[dict] = []

    def fetch(_settings, *, uidvalidity, last_uid, now) -> Fetched:
        calls.append({"uidvalidity": uidvalidity, "last_uid": last_uid})
        return Fetched(uidvalidity=7, messages=[m for m in messages if m[0] > (last_uid or 0)])

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


# -------------------------------------------------------------------- parse


def test_parse_plain_requirement() -> None:
    parsed = parse_message(mail("requirement.eml"))
    assert parsed.message_id == "<req-4711@odysseytec.example>"
    assert (parsed.from_name, parsed.from_addr) == ("Sai Kumar", "sai.kumar@odysseytec.example")
    assert parsed.date == datetime(2026, 9, 30, 14, 15, tzinfo=UTC)
    assert not parsed.is_bounce and not parsed.auto_submitted
    assert looks_like_requirement(parsed)

    requirement = extract_requirement(parsed)
    assert requirement.title == "Cloud Architect"
    assert requirement.vendor == "Odysseytec"
    assert requirement.client == "Southwind Air"
    assert requirement.location == "Remote (quarterly travel to Dallas, TX)"
    assert requirement.remote is True
    assert requirement.employment_type == "contract"
    assert requirement.duration == "12+ months"
    assert requirement.comp == Comp(95, 105, "USD", "hour")
    assert requirement.contact_email == "sai.kumar@odysseytec.example"
    assert "Kubernetes (EKS) platform architecture" in requirement.description


def test_parse_html_requirement_and_confidential_client() -> None:
    parsed = parse_message(mail("requirement_html.eml"))
    assert looks_like_requirement(parsed)
    requirement = extract_requirement(parsed)
    assert requirement.title == "Sr. AWS Cloud Engineer"
    assert requirement.location == "Dallas, TX (Hybrid)"
    assert requirement.remote is False
    assert requirement.client == ""  # "Confidential" is not a client name
    assert requirement.employment_type == "contract"
    assert requirement.vendor == "Insightglobal"


@pytest.mark.parametrize(
    ("subject", "title"),
    [
        ("Urgent requirement: Cloud Architect (Remote) || C2C", "Cloud Architect"),
        ("Re: Fwd: Job Opportunity - DevOps Engineer - Austin, TX", "DevOps Engineer"),
        ("Immediate need for Kubernetes Architect | Remote | 6 months", "Kubernetes Architect"),
        ("Platform Engineer at Acme", "Platform Engineer"),
        ("Hiring: Sr. SRE", "Sr. SRE"),
    ],
)
def test_title_from_subject(subject: str, title: str) -> None:
    raw = (
        f"From: R <r@vendor.example>\nSubject: {subject}\nMessage-ID: <x@vendor.example>\n\n"
        "Location: Remote\nDuration: 6 months\nC2C. Please share your resume.\n"
    ).encode()
    assert extract_requirement(parse_message(raw)).title == title


def test_other_mail_is_not_a_requirement() -> None:
    assert not looks_like_requirement(parse_message(mail("newsletter.eml")))
    assert parse_message(mail("bounce.eml")).is_bounce
    assert parse_message(mail("autoreply.eml")).auto_submitted


def test_freemail_sender_uses_the_display_name_as_vendor() -> None:
    raw = b"From: Rita Recruiter <rita@gmail.com>\nSubject: Role\nMessage-ID: <1@gmail.com>\n\nHi\n"
    assert extract_requirement(parse_message(raw)).vendor == "Rita Recruiter"


# ------------------------------------------------------------------- ingest


def test_ingest_creates_vendor_jobs_once(
    session: Session, user: User, user_config: UserConfig, settings: Settings
) -> None:
    fetch = fetcher(
        [
            (11, mail("requirement.eml")),
            (12, mail("newsletter.eml")),
            (13, mail("requirement_html.eml")),
        ]
    )

    stats = ingest_inbox(session, settings, user_config, user, fetch=fetch, now=NOW)
    assert (stats.read, stats.requirements, stats.ignored) == (3, 2, 1)

    jobs = {job.title: job for job in session.scalars(select(Job))}
    job = jobs["Cloud Architect"]
    assert job.company_name == "Odysseytec" and job.client_name == "Southwind Air"
    assert job.contact_email == "sai.kumar@odysseytec.example" and job.contact_name == "Sai Kumar"
    assert job.employment_type == "contract" and job.remote is True
    assert (job.comp_min, job.comp_max, job.comp_period) == (95.0, 105.0, "hour")
    assert job.facts["years_required"] == 10
    assert job.raw["message_id"] == "<req-4711@odysseytec.example>" and job.raw["vendor"] is True
    assert job.is_backfill is False and job.posted_at == datetime(2026, 9, 30, 14, 15, tzinfo=UTC)
    assert choose_channel(job).value == "email"
    assert ledger.parties(job) == ("Southwind Air", "Odysseytec")
    # It scores in the contract lane like any other job.
    result = score_job(job, user_config.search, now=NOW, profile=user_config.profile)
    assert result.lane == "contract" and result.decision.value == "shortlist"

    source = session.scalar(select(Source).where(Source.kind == "email"))
    assert source.config == {"uidvalidity": 7, "last_uid": 13}
    assert [m.kind for m in session.scalars(select(InboundEmail).order_by(InboundEmail.id))] == [
        "requirement",
        "requirement",
    ]

    again = ingest_inbox(session, settings, user_config, user, fetch=fetch, now=NOW)
    assert again.read == 0 and fetch.calls[-1] == {"uidvalidity": 7, "last_uid": 13}
    assert len(session.scalars(select(Job)).all()) == 2


def _sent(session: Session, user: User) -> Application:
    source = Source(kind="email", token="email", initialized=True)
    session.add(source)
    session.flush()
    job = Job(
        source_id=source.id, external_id="x", company_name="Odysseytec", company_key="odysseytec",
        title="Cloud Architect", fingerprint="f", first_seen_at=NOW, last_seen_at=NOW,
    )  # fmt: skip
    session.add(job)
    session.flush()
    application = Application(
        user_id=user.id, job_id=job.id, channel="email", status="submitted", submitted_at=NOW,
        follow_up_at=NOW + timedelta(days=7),
    )  # fmt: skip
    session.add(application)
    session.flush()
    session.add(
        OutboundEmail(
            application_id=application.id,
            to_addr="sai.kumar@odysseytec.example",
            subject="s",
            body="b",
            message_id=OUR_ID,
            status="sent",
            sent_at=NOW,
        )
    )
    session.commit()
    return application


def test_a_reply_moves_the_application_and_is_not_a_new_job(
    session: Session, user: User, user_config: UserConfig, settings: Settings
) -> None:
    application = _sent(session, user)
    stats = ingest_inbox(
        session, settings, user_config, user, fetch=fetcher([(21, mail("reply.eml"))]), now=NOW
    )
    assert (stats.replies, stats.requirements) == (1, 0)
    session.refresh(application)
    assert application.status == "replied"
    assert application.next_action == "Reply to Sai Kumar" and application.follow_up_at is None
    assert application.events[-1].kind == "reply_received"
    assert session.scalar(select(InboundEmail)).application_id == application.id


def test_auto_reply_does_not_count_as_a_reply(
    session: Session, user: User, user_config: UserConfig, settings: Settings
) -> None:
    application = _sent(session, user)
    stats = ingest_inbox(
        session, settings, user_config, user, fetch=fetcher([(22, mail("autoreply.eml"))]), now=NOW
    )
    session.refresh(application)
    assert application.status == "submitted" and stats.replies == 0
    assert application.events[-1].kind == "auto_reply"


def test_a_bounce_marks_the_application_failed(
    session: Session, user: User, user_config: UserConfig, settings: Settings
) -> None:
    application = _sent(session, user)
    stats = ingest_inbox(
        session, settings, user_config, user, fetch=fetcher([(23, mail("bounce.eml"))]), now=NOW
    )
    session.refresh(application)
    assert stats.bounces == 1
    assert application.status == "failed"
    assert "bounced" in application.error and application.follow_up_at is None


def test_own_copies_and_broken_messages_are_skipped_without_blocking(
    session: Session, user: User, user_config: UserConfig, settings: Settings
) -> None:
    own = b"From: Alex Example <alex@example.com>\nSubject: Re: Cloud Architect requirement\nMessage-ID: <own@example.com>\n\nLocation: Remote. Duration: 6 months. C2C. Resume attached.\n"
    fetch = fetcher(
        [(31, own), (32, b""), (33, b"\xff\xfe not a message"), (34, mail("requirement.eml"))]
    )
    stats = ingest_inbox(session, settings, user_config, user, fetch=fetch, now=NOW)
    assert (stats.read, stats.requirements, stats.ignored) == (4, 1, 3)
    assert session.scalar(select(Source).where(Source.kind == "email")).config["last_uid"] == 34


# --------------------------------------------------------------------- imap


class FakeImap:
    """Records the commands issued; never allows a write."""

    instances: list[FakeImap] = []

    def __init__(self, host: str, port: int, timeout: int = 0) -> None:
        self.host, self.port, self.commands = host, port, []
        FakeImap.instances.append(self)

    def login(self, username: str, password: str):
        self.commands.append(("LOGIN", username))
        if password == "wrong":
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False):
        self.commands.append(("SELECT", folder, readonly))
        return ("OK", [b"3"]) if "Recruiters" in folder else ("NO", [b"no such folder"])

    def response(self, name: str):
        return name, [b"7"]

    def uid(self, command: str, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"41 42 43"]
        uid = args[0]
        return "OK", [(f"{uid} (UID {uid} BODY[] {{12}}".encode(), f"message {uid}".encode()), b")"]

    def logout(self):
        self.commands.append(("LOGOUT",))
        return "BYE", [b""]


@pytest.fixture
def imap(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> type[FakeImap]:
    from pydantic import SecretStr

    FakeImap.instances = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    settings.imap_host, settings.imap_username = "imap.example.com", "alex@example.com"
    settings.imap_password = SecretStr("app-password")
    settings.imap_folder = "Recruiters"
    return FakeImap


def test_imap_is_read_only_and_peeks(settings: Settings, imap: type[FakeImap]) -> None:
    fetched = fetch_new(settings, uidvalidity=None, last_uid=None, now=NOW)
    assert fetched.uidvalidity == 7
    assert fetched.messages == [(41, b"message 41"), (42, b"message 42"), (43, b"message 43")]
    commands = imap.instances[0].commands
    assert ("SELECT", '"Recruiters"', True) in commands  # opened read-only
    assert ("SEARCH", None, "SINCE 16-Sep-2026") in commands  # first run: two weeks back
    assert all(c[2] == "(BODY.PEEK[])" for c in commands if c[0] == "FETCH")  # nothing marked read
    assert commands[-1] == ("LOGOUT",)
    assert not any(c[0] in ("STORE", "COPY", "MOVE", "EXPUNGE", "APPEND") for c in commands)


def test_imap_resumes_after_the_last_uid(settings: Settings, imap: type[FakeImap]) -> None:
    fetched = fetch_new(settings, uidvalidity=7, last_uid=42, now=NOW)
    assert [uid for uid, _ in fetched.messages] == [43]
    assert ("SEARCH", None, "UID 43:*") in imap.instances[0].commands
    # The server renumbered the folder: start over from the date window.
    fetch_new(settings, uidvalidity=6, last_uid=42, now=NOW)
    assert ("SEARCH", None, "SINCE 16-Sep-2026") in imap.instances[1].commands


def test_imap_errors_are_reported(settings: Settings, imap: type[FakeImap]) -> None:
    from pydantic import SecretStr

    settings.imap_folder = "Nope"
    with pytest.raises(InboxError, match="could not be opened"):
        fetch_new(settings, uidvalidity=None, last_uid=None, now=NOW)
    settings.imap_password = SecretStr("wrong")
    with pytest.raises(InboxError, match="refused the login"):
        fetch_new(settings, uidvalidity=None, last_uid=None, now=NOW)
    assert imap.instances[-1].commands[-1] == ("LOGOUT",)
    settings.imap_host = None
    with pytest.raises(InboxError, match="not configured"):
        fetch_new(settings, uidvalidity=None, last_uid=None, now=NOW)


def test_fixture_paths_exist() -> None:
    assert Path(FIXTURES / "mail" / "requirement.eml").exists()
