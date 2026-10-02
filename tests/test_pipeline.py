from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Browser
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from jobportal.apply.service import add_event, approve
from jobportal.browser import LazyBrowser
from jobportal.cli import app
from jobportal.config import UserConfig, load_user_config
from jobportal.crawl import add_source
from jobportal.db import get_session_factory, utcnow
from jobportal.http import PoliteClient
from jobportal.inbox.ingest import ingest_inbox
from jobportal.manual import ManualJobError, add_manual_job, hosted_form_url
from jobportal.models import Application, Job, JobScore, LedgerEntry, Source, User
from jobportal.pipeline import RunSummary, make_transport, run_once
from jobportal.settings import Settings
from jobportal.sources import SourceSpec
from jobportal.users import get_default_user
from jobportal.worker import Worker
from tests.conftest import FIXTURES, NOW, CapturedMail, FakeWeb, fixture_json
from tests.formserver import FormServer

GH_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


def _requirement_fetch(_settings, *, uidvalidity, last_uid, now):
    from jobportal.inbox.imap import Fetched

    raw = (FIXTURES / "mail" / "requirement.eml").read_bytes()
    return Fetched(uidvalidity=1, messages=[] if last_uid else [(5, raw)])


@pytest.mark.browser
def test_full_pass_in_review_mode(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    client: PoliteClient,
    web: FakeWeb,
    browser: Browser,
    form_server: FormServer,
    smtp_server: CapturedMail,
) -> None:
    lazy = LazyBrowser(settings, browser=browser)
    transport = make_transport(settings)
    add_source(session, SourceSpec(kind="greenhouse", token="acme"))
    session.commit()
    web.json("GET", GH_URL, fixture_json("greenhouse_jobs.json"))
    ingest_inbox(session, settings, user_config, user, fetch=_requirement_fetch, now=NOW)

    # Pass 1 reads the board. Its postings are a backlog (first read), so point
    # the one that matters at the local stand-in form before preparing.
    first = run_once(
        session, settings, user_config, client=client, browser=lazy, transport=transport, now=NOW
    )
    assert first.scores.shortlisted == 2 and first.sent == 0
    assert first.prepared  # both shortlisted roles were prepared straight away
    principal = session.scalar(select(Job).where(Job.title == "Principal Platform Engineer"))
    assert principal.apply_url.startswith("https://job-boards.greenhouse.io/embed/job_app")
    # The real form address is not reachable from the test machine: the page
    # fails to load and the application is handed over rather than guessed at.
    blocked = session.scalar(select(Application).where(Application.job_id == principal.id))
    assert blocked.status == "needs_human"

    # Re-point it at the stand-in and ask for it again.
    principal.apply_url = form_server.url("classic.html")
    blocked.status = "preparing"
    session.commit()
    second = run_once(
        session, settings, user_config, client=client, browser=lazy, transport=transport,
        now=NOW + timedelta(minutes=1), do_crawl=False,
    )  # fmt: skip
    assert second.prepared == {"needs_review": 1}

    waiting = session.scalars(select(Application).where(Application.status == "needs_review")).all()
    assert sorted(a.channel for a in waiting) == ["email", "form"]
    assert smtp_server.envelopes == [] and form_server.posts() == []  # nothing has gone out

    for application in waiting:
        approve(application, now=NOW + timedelta(minutes=2))
    session.commit()
    third = run_once(
        session, settings, user_config, client=client, browser=lazy, transport=transport,
        now=NOW + timedelta(minutes=3), do_crawl=False,
    )  # fmt: skip
    assert third.sent == 2 and third.errors == []
    assert len(smtp_server.envelopes) == 1 and len(form_server.posts()) == 1
    assert "Sent 2." in third.lines()
    ledger_rows = {(e.client_name, e.vendor_name) for e in session.scalars(select(LedgerEntry))}
    assert ledger_rows == {("Acme Robotics", ""), ("Southwind Air", "Odysseytec")}

    # Nothing is sent twice.
    fourth = run_once(
        session, settings, user_config, client=client, browser=lazy, transport=transport,
        now=NOW + timedelta(minutes=5), do_crawl=False,
    )  # fmt: skip
    assert fourth.sent == 0 and fourth.prepared == {}
    assert len(smtp_server.envelopes) == 1 and len(form_server.posts()) == 1


@pytest.mark.browser
def test_hidden_jobs_are_not_prepared_and_prepare_is_capped(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    client: PoliteClient,
    browser: Browser,
) -> None:
    config = user_config.model_copy(deep=True)
    config.search.policy.prepare_per_run = 2
    for index in range(4):
        add_manual_job(
            session,
            title=f"Principal Platform Engineer {index}",
            company=f"Company {index}",
            description="Kubernetes platform on AWS with Terraform, GitOps, Kafka and Go.",
            location="Remote - US",
            contact_email=f"jobs{index}@example.org",
            now=NOW,
        )
    session.commit()
    lazy = LazyBrowser(settings, browser=browser)
    first = run_once(
        session, settings, config, client=client, browser=lazy, transport=None, now=NOW
    )
    assert first.scores.shortlisted == 4
    assert sum(first.prepared.values()) == 2  # capped per run

    hidden = session.scalars(
        select(JobScore).where(~JobScore.job_id.in_(select(Application.job_id)))
    ).first()
    hidden.hidden = True
    session.commit()
    second = run_once(
        session, settings, config, client=client, browser=lazy, transport=None, now=NOW
    )
    assert sum(second.prepared.values()) == 1  # the hidden one is left alone
    assert session.scalar(select(Application).where(Application.job_id == hidden.job_id)) is None


def test_summary_reads_plainly() -> None:
    assert RunSummary().lines() == ["Nothing to do."]


# ------------------------------------------------------------------ manual


def test_add_manual_job(session: Session) -> None:
    job = add_manual_job(
        session,
        title=" Platform  Architect ",
        company="Globex",
        url="https://jobs.lever.co/globex/681fbc53-1e34-4a46-8677-3a78118674eb",
        description="This is a fully remote role. Pay: $190,000 - $240,000. 12+ years of experience.",
        now=NOW,
    )
    assert job.title == "Platform Architect" and job.source.kind == "manual"
    assert (
        job.apply_url == "https://jobs.lever.co/globex/681fbc53-1e34-4a46-8677-3a78118674eb/apply"
    )
    assert job.remote is True and (job.comp_min, job.comp_max) == (190000.0, 240000.0)
    assert job.facts["years_required"] == 12 and job.is_backfill is False

    vendor = add_manual_job(
        session, title="Cloud Architect", company="TekVendor", client_name="Big Bank",
        contact_email="r@tek.example", via_vendor=True, now=NOW,
    )  # fmt: skip
    assert vendor.employment_type == "contract" and vendor.raw == {"vendor": True}
    assert vendor.apply_url is None

    for bad in (
        {"title": "", "company": "X"},
        {"title": "X", "company": "Y", "url": "ftp://x"},
        {"title": "X", "company": "Y", "contact_email": "nope"},
        {"title": "X", "company": "Y", "employment_type": "gig"},
    ):
        with pytest.raises(ManualJobError):
            add_manual_job(session, now=NOW, **bad)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://job-boards.greenhouse.io/acme/jobs/8172508",
            "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=8172508",
        ),
        (
            "https://boards.greenhouse.io/acme/jobs/8172508?gh_src=x",
            "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=8172508",
        ),
        ("https://jobs.lever.co/globex/abc/apply", "https://jobs.lever.co/globex/abc/apply"),
        (
            "https://jobs.ashbyhq.com/initech/abc",
            "https://jobs.ashbyhq.com/initech/abc/application",
        ),
        ("https://jobs.ashbyhq.com/initech", None),
        ("https://example.com/careers/123", None),
    ],
)
def test_hosted_form_url(url: str, expected: str | None) -> None:
    assert hosted_form_url(url) == expected


# ------------------------------------------------------------------ worker


@pytest.mark.browser
def test_worker_tick_sends_approved_and_survives_bad_config(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    data_dir: Path,
    browser: Browser,
    web: FakeWeb,
    smtp_server: CapturedMail,
) -> None:
    job = add_manual_job(
        session, title="Principal Platform Engineer", company="Acme Robotics",
        description="Kubernetes platform on AWS with Terraform, GitOps, Kafka and Go.",
        location="Remote - US", contact_email="jobs@acme.example", now=NOW,
    )  # fmt: skip
    session.commit()

    worker = Worker(
        settings,
        browser_factory=lambda: LazyBrowser(settings, browser=browser),
        client_factory=lambda: PoliteClient(settings, transport=httpx.MockTransport(web.handler)),
    )
    summary = worker.tick(now=NOW)
    assert summary is not None and summary.prepared == {"needs_review": 1}

    session.expire_all()
    application = session.scalar(select(Application).where(Application.job_id == job.id))
    approve(application, now=NOW)
    session.commit()

    search = data_dir / "search.yaml"
    good = search.read_text()
    search.write_text(good.replace("mode: review", "mode: sometimes"))
    assert worker.tick(now=NOW + timedelta(seconds=30)) is None  # broken rules: nothing is sent
    assert "search.yaml is not valid" in worker.last_error and smtp_server.envelopes == []

    search.write_text(good)
    summary = worker.tick(now=NOW + timedelta(seconds=60))
    assert summary is not None and summary.sent == 1 and worker.last_error == ""
    assert len(smtp_server.envelopes) == 1


@pytest.mark.browser
def test_worker_prepares_and_sends_even_when_reading_fails(
    session: Session,
    user: User,
    user_config: UserConfig,
    settings: Settings,
    data_dir: Path,
    browser: Browser,
    web: FakeWeb,
    smtp_server: CapturedMail,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    from jobportal import pipeline as pipeline_module
    from jobportal import worker as worker_module

    add_manual_job(
        session, title="Principal Platform Engineer", company="Acme Robotics",
        description="Kubernetes platform on AWS with Terraform, GitOps, Kafka and Go.",
        location="Remote - US", contact_email="jobs@acme.example", now=NOW,
    )  # fmt: skip
    session.commit()
    settings.imap_host, settings.imap_username = "imap.example.com", "alex@example.com"
    settings.imap_password = SecretStr("app-password")
    crawls: list[object] = []

    def broken_mailbox(*_args: object, **_kwargs: object) -> None:
        raise ValueError("invalid literal for int() with base 10: b'abc'")

    def broken_crawl(*_args: object, **kwargs: object) -> None:
        crawls.append(kwargs.get("min_interval"))
        raise AttributeError("'dict' object has no attribute 'strip'")

    monkeypatch.setattr(worker_module, "ingest_inbox", broken_mailbox)
    monkeypatch.setattr(pipeline_module, "crawl", broken_crawl)
    worker = Worker(
        settings,
        browser_factory=lambda: LazyBrowser(settings, browser=browser),
        client_factory=lambda: PoliteClient(settings, transport=httpx.MockTransport(web.handler)),
    )

    summary = worker.tick(now=NOW)
    assert summary is not None  # the pass was not abandoned
    assert summary.prepared == {"needs_review": 1}
    assert any("Mailbox" in error for error in summary.errors)
    assert any("Sources" in error for error in summary.errors)

    # The failed reading is not repeated every few seconds ...
    worker.tick(now=NOW + timedelta(seconds=15))
    assert len(crawls) == 1
    # ... but "Read them now" means now, and means every source.
    worker.request_crawl()
    worker.tick(now=NOW + timedelta(seconds=30))
    assert len(crawls) == 2 and crawls[-1] is None


# --------------------------------------------------------------------- cli

runner = CliRunner()


def test_cli_init_check_and_sources(settings: Settings, tmp_path: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (settings.data_dir / "profile.yaml").exists()
    assert "created" in result.output
    assert "kept" in runner.invoke(app, ["init"]).output  # never overwrites your files

    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "Alex Example" in result.output
    assert "review: nothing goes out without your approval" in result.output
    assert "not configured: email applications are saved as drafts" in result.output

    boards = tmp_path / "boards.txt"
    boards.write_text(
        "# my list\nhttps://boards.greenhouse.io/acme, Acme Robotics\n"
        "https://jobs.lever.co/globex\nhttps://example.com/careers\n\n"
    )
    result = runner.invoke(app, ["sources", "import", str(boards)])
    assert "Added 2 sources (1 lines skipped)" in result.output
    listing = runner.invoke(app, ["sources", "list"]).output
    assert "greenhouse" in listing and "Acme Robotics" in listing and "never" in listing
    assert runner.invoke(app, ["sources", "add", "jobs.ashbyhq.com/initech"]).exit_code == 0
    assert "Removed #1" in runner.invoke(app, ["sources", "remove", "1"]).output
    with get_session_factory()() as check:
        assert [s.kind for s in check.scalars(select(Source).order_by(Source.id))] == [
            "lever",
            "ashby",
        ]


def test_cli_since_lists_what_was_sent_and_who_replied(settings: Settings) -> None:
    runner.invoke(app, ["init"])
    with get_session_factory()() as session:
        user = get_default_user(session, load_user_config(settings.data_dir).profile)
        job = add_manual_job(
            session, title="Cloud Architect", company="Odyssey Staffing", description="AWS.",
            contact_email="sai@odyssey.example", now=utcnow(),
        )  # fmt: skip
        application = Application(
            user_id=user.id, job_id=job.id, channel="email", status="replied", submitted_at=utcnow()
        )
        session.add(application)
        session.flush()
        add_event(application, "reply_received", sender="sai@odyssey.example", subject="Re: role")
        session.commit()
    output = runner.invoke(app, ["since"]).output
    assert "1 new postings, 1 applications sent, 1 replies" in output
    assert "sent (by you): Cloud Architect - Odyssey Staffing" in output
    assert "reply from sai@odyssey.example: Cloud Architect - Odyssey Staffing" in output


def test_cli_reports_config_errors_plainly(settings: Settings) -> None:
    runner.invoke(app, ["init"])
    path = settings.data_dir / "search.yaml"
    data = yaml.safe_load(path.read_text())
    data["policy"]["auto"]["min_scroe"] = 70
    path.write_text(yaml.safe_dump(data))
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "policy.auto.min_scroe" in result.output


def test_cli_score_jobs_queue_and_approve(settings: Settings) -> None:
    runner.invoke(app, ["init"])
    with get_session_factory()() as session:
        add_manual_job(
            session, title="Principal Platform Engineer", company="Acme Robotics",
            description="Kubernetes platform on AWS with Terraform, GitOps, Kafka and Go.",
            location="Remote - US", contact_email="jobs@acme.example",
        )  # fmt: skip
        session.commit()
    assert "1 shortlisted" in runner.invoke(app, ["score"]).output
    listing = runner.invoke(app, ["jobs"]).output
    assert "Principal Platform Engineer - Acme Robotics" in listing
    assert "Nothing is waiting on you." in runner.invoke(app, ["queue"]).output

    with get_session_factory()() as session:
        job = session.scalar(select(Job))
        session.add(Application(user_id=1, job_id=job.id, channel="email", status="needs_review"))
        session.commit()
    assert "needs_review" in runner.invoke(app, ["queue"]).output
    assert runner.invoke(app, ["approve"]).exit_code == 1  # needs ids or --all
    assert "approved #1" in runner.invoke(app, ["approve", "--all"]).output
    assert "approved" in runner.invoke(app, ["queue"]).output
