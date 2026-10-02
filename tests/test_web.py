from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.config import UserConfig
from jobportal.db import get_session_factory, utcnow
from jobportal.manual import add_manual_job
from jobportal.models import (
    Answer,
    Application,
    Job,
    JobScore,
    LedgerEntry,
    ResumeVariant,
    Source,
    User,
)
from jobportal.scoring import score_jobs
from jobportal.settings import Settings
from jobportal.web.app import create_app
from jobportal.web.security import require_safe_binding
from tests.conftest import NOW

LOCAL = ("127.0.0.1", 50000)

DESCRIPTION = (
    "Own the architecture of our Kubernetes platform on AWS with Terraform and GitOps. "
    "Zero trust with Vault, operators in Go, Kafka. 10+ years of experience. Up to 20% travel. "
    "Base salary $210,000 - $265,000."
)


@pytest.fixture
def app_client(settings: Settings, session: Session, user: User) -> TestClient:
    settings.allowed_hosts = ["testserver"]
    # No password is set, so the app only answers this machine itself.
    return TestClient(create_app(settings), follow_redirects=False, client=LOCAL)


@pytest.fixture
def jobs(session: Session, user: User, user_config: UserConfig) -> dict[str, Job]:
    # The routes read the real clock, so these are stamped with it too: a fixed
    # date here would age out of the "posted in the last N days" filters.
    now = utcnow()
    made = {
        "principal": add_manual_job(
            session, title="Principal Platform Engineer", company="Acme Robotics",
            description=DESCRIPTION, location="Remote - US", url="https://example.com/acme/1", now=now,
        ),
        "contract": add_manual_job(
            session, title="Cloud Architect", company="Odyssey Staffing", client_name="Southwind Air",
            description="AWS landing zones with Terraform, Python, Kubernetes, CI/CD. Rate: $95 - $110 per hour.",
            location="Dallas, TX (Hybrid)", contact_email="sai@odyssey.example", contact_name="Sai Kumar",
            via_vendor=True, now=now,
        ),
        "sales": add_manual_job(
            session, title="Account Executive <script>alert(1)</script>", company="Globex",
            description="Sell things.", location="Dublin", now=now,
        ),
    }  # fmt: skip
    score_jobs(session, user.id, user_config.search, now=now, profile=user_config.profile)
    session.commit()
    return made


def _application(session: Session, user: User, job: Job, status: str, **fields) -> Application:
    application = Application(
        user_id=user.id,
        job_id=job.id,
        channel=fields.pop("channel", "email"),
        status=status,
        **fields,
    )
    session.add(application)
    session.commit()
    return application


def _fresh(model, ident):
    with get_session_factory()() as check:
        return check.get(model, ident)


# --------------------------------------------------------------------- feed


def test_feed_lists_scored_roles_with_facts(app_client: TestClient, jobs: dict[str, Job]) -> None:
    assert app_client.get("/").headers["location"] == "/feed"
    page = app_client.get("/feed")
    assert page.status_code == 200
    text = page.text
    assert "Principal Platform Engineer" in text and "Cloud Architect" in text
    assert "Account Executive" not in text  # skipped roles are not in the default views
    assert "$210k-265k a year" in text and "$95-110 an hour" in text
    assert "10+ years" in text and "20% travel" in text and "Hybrid" in text
    assert "for Southwind Air" in text


def test_skipped_roles_show_their_reason_and_markup_is_escaped(
    app_client: TestClient, jobs: dict[str, Job]
) -> None:
    text = app_client.get("/feed?view=all").text
    assert "Skipped" in text
    assert "matches none of this lane" in text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


@pytest.mark.parametrize(
    ("query", "present", "absent"),
    [
        ("workplace=hybrid", "Cloud Architect", "Principal Platform Engineer"),
        ("workplace=remote", "Principal Platform Engineer", "Cloud Architect"),
        ("lane=contract", "Cloud Architect", "Principal Platform Engineer"),
        ("commitment=contract", "Cloud Architect", "Principal Platform Engineer"),
        ("q=acme", "Principal Platform Engineer", "Cloud Architect"),
        ("q=100%25", "No roles match these filters", "Principal Platform Engineer"),
        ("min_pay=200000", "Principal Platform Engineer", "Cloud Architect"),
        ("min_pay=100", "Cloud Architect", "Principal Platform Engineer"),
        ("posted=1&min_pay=&lane=", "Principal Platform Engineer", "No roles match"),
        ("page=abc&posted=zzz", "Principal Platform Engineer", "No roles match"),
    ],
)
def test_feed_filters(
    app_client: TestClient, jobs: dict[str, Job], query: str, present: str, absent: str
) -> None:
    text = app_client.get(f"/feed?view=shortlist&{query}").text
    assert present in text and absent not in text


def test_job_page_says_why_a_good_score_is_not_shortlisted(
    app_client: TestClient, session: Session, user: User, user_config: UserConfig
) -> None:
    job = add_manual_job(
        session, title="Principal Platform Engineer", company="Acme Robotics",
        description=DESCRIPTION, location="Smallville", url="https://example.com/acme/9",
        now=utcnow(),
    )  # fmt: skip
    score_jobs(session, user.id, user_config.search, now=utcnow(), profile=user_config.profile)
    session.commit()
    text = app_client.get(f"/jobs/{job.id}").text
    assert "Could not tell where this is (Smallville)" in text
    assert "Kept off the shortlist" in text and "On the shortlist" not in text


def test_pay_filter_compares_like_with_like(
    app_client: TestClient, session: Session, jobs: dict[str, Job]
) -> None:
    def listed() -> bool:
        return "Principal Platform Engineer" in app_client.get("/feed?view=all&min_pay=180000").text

    job = jobs["principal"]
    assert listed()
    job.comp_min, job.comp_max, job.comp_currency = 110000.0, 185000.0, "EUR"
    session.commit()
    assert not listed()  # euros are not compared with a floor in dollars
    job.comp_currency = "usd"
    session.commit()
    assert listed()
    job.comp_min, job.comp_max = 200000.0, None  # "from $200,000": only a minimum is given
    session.commit()
    assert listed()
    job.comp_min = 150000.0
    session.commit()
    assert not listed()


def test_pay_label_names_the_period_only_when_it_is_known() -> None:
    from jobportal.web.deps import pay

    job = Job(comp_min=8000.0, comp_max=10000.0, comp_currency="USD", comp_period=None)
    assert pay(job) == "$8k-10k"
    job.comp_period = "year"
    assert pay(job) == "$8k-10k a year"
    assert pay(Job(comp_min=95.0, comp_max=110.0, comp_currency="usd", comp_period="hour")) == (
        "$95-110 an hour"
    )
    assert pay(Job(comp_max=60.0, comp_currency="SGD", comp_period="hour")) == "60 SGD an hour"
    assert pay(Job()) == ""


def test_empty_feed_points_to_sources(app_client: TestClient) -> None:
    text = app_client.get("/feed").text
    assert "No roles yet" in text and 'href="/sources"' in text


def test_job_page_explains_the_score(app_client: TestClient, jobs: dict[str, Job]) -> None:
    text = app_client.get(f"/jobs/{jobs['principal'].id}").text
    assert "On the shortlist for Career roles" in text
    assert "Title matches target" in text and "of 30" in text
    assert "Own the architecture of our Kubernetes platform" in text
    assert "Open the original posting" in text
    skipped = app_client.get(f"/jobs/{jobs['sales'].id}").text
    assert "Skipped" in skipped and "You can still apply" in skipped
    assert app_client.get("/jobs/99999").status_code == 404


def test_save_and_hide(app_client: TestClient, jobs: dict[str, Job], user: User) -> None:
    job_id = jobs["principal"].id
    response = app_client.post(f"/jobs/{job_id}/save")
    assert response.status_code == 303
    with get_session_factory()() as check:
        assert check.scalar(select(JobScore).where(JobScore.job_id == job_id)).saved is True
    assert "Principal Platform Engineer" in app_client.get("/feed?view=saved").text

    # In-page update: only the row comes back.
    row = app_client.post(
        f"/jobs/{job_id}/hide", headers={"HX-Request": "true", "HX-Target": f"job-{job_id}"}
    )
    assert row.status_code == 200 and row.text.lstrip().startswith("<li")
    assert "Bring back" in row.text and "<html" not in row.text
    assert "Principal Platform Engineer" not in app_client.get("/feed?view=shortlist").text
    assert "Principal Platform Engineer" in app_client.get("/feed?view=hidden").text


def test_apply_queues_the_application(app_client: TestClient, jobs: dict[str, Job]) -> None:
    job_id = jobs["principal"].id
    app_client.post(f"/jobs/{job_id}/apply")
    with get_session_factory()() as check:
        application = check.scalar(select(Application).where(Application.job_id == job_id))
        assert application.status == "preparing"
    queue = app_client.get("/queue").text
    assert "In progress" in queue and "being prepared" in queue


def test_already_applied_goes_straight_to_the_tracker_and_ledger(
    app_client: TestClient, jobs: dict[str, Job]
) -> None:
    app_client.post(f"/jobs/{jobs['principal'].id}/applied")
    tracker = app_client.get("/tracker").text
    assert "Principal Platform Engineer" in tracker and "By you" in tracker
    assert "Acme Robotics" in app_client.get("/ledger").text


def test_add_a_role_by_hand(app_client: TestClient, user_config: UserConfig) -> None:
    assert "Add a role" in app_client.get("/jobs/new").text
    bad = app_client.post("/jobs", data={"title": "", "company": "X"})
    assert bad.status_code == 400 and "A title and a company are required" in bad.text
    created = app_client.post(
        "/jobs",
        data={
            "title": "Staff Platform Engineer",
            "company": "Initech",
            "location": "Remote - US",
            "description": DESCRIPTION,
            "url": "https://jobs.ashbyhq.com/initech/abc",
        },
    )
    assert created.status_code == 303
    page = app_client.get(created.headers["location"]).text
    assert "Staff Platform Engineer" in page and "On the shortlist" in page


# -------------------------------------------------------------------- queue


def test_queue_review_edit_and_approve(
    app_client: TestClient, session: Session, user: User, jobs: dict[str, Job]
) -> None:
    application = _application(
        session, user, jobs["contract"], "needs_review",
        prepared={"channel": "email", "to": "sai@odyssey.example", "subject": "Re: Cloud Architect", "body": "Hello,\n\nOriginal.\n", "attachments": ["/x/Alex_Example_Resume.pdf"], "auto_notes": []},
    )  # fmt: skip
    page = app_client.get("/queue").text
    assert "Ready for your approval" in page and "By email to sai@odyssey.example" in page
    assert "Review mode: nothing goes out until you approve it here." in page
    assert '<span class="count"' in page  # the nav shows something is waiting

    app_client.post(
        f"/applications/{application.id}/draft",
        data={"subject": "Cloud Architect - Alex", "body": "Edited once."},
    )
    assert _fresh(Application, application.id).status == "needs_review"

    response = app_client.post(
        f"/applications/{application.id}/approve",
        data={"subject": "Cloud Architect - Alex Example", "body": "Hi Sai,\n\nFinal text."},
    )
    assert response.status_code == 303
    approved = _fresh(Application, application.id)
    assert approved.status == "approved" and approved.auto is False
    assert approved.prepared["subject"] == "Cloud Architect - Alex Example"
    assert approved.prepared["body"] == "Hi Sai,\n\nFinal text.\n"

    again = app_client.post(f"/applications/{application.id}/approve", follow_redirects=True)
    assert "Only applications waiting for review can be approved" in again.text


def test_queue_answers_are_saved_to_the_bank(
    app_client: TestClient, session: Session, user: User, jobs: dict[str, Job]
) -> None:
    application = _application(
        session, user, jobs["principal"], "needs_answers", channel="form",
        prepared={
            "channel": "form",
            "fields": [],
            "unanswered": [
                {"key": "why do you want to work here", "label": "Why do you want to work here?", "type": "textarea", "required": True, "options": []},
                {"key": "which clouds", "label": "Which clouds?", "type": "checkbox", "required": True, "options": ["AWS", "Azure", "GCP"]},
                {"key": "privacy policy", "label": "I agree to the privacy policy", "type": "checkbox", "required": True, "options": ["Yes"]},
            ],
        },
    )  # fmt: skip
    page = app_client.get("/queue").text
    assert "Needs your answer" in page and "Why do you want to work here?" in page
    assert 'type="checkbox" name="answer:which clouds" value="AWS"' in page

    response = app_client.post(
        f"/applications/{application.id}/answers",
        data={
            "answer:why do you want to work here": "The platform roadmap.",
            "answer:which clouds": ["AWS", "GCP"],
            "answer:privacy policy": "Yes",
            "answer:not-a-question": "ignored",
        },
    )
    assert response.status_code == 303
    assert _fresh(Application, application.id).status == "preparing"
    with get_session_factory()() as check:
        bank = {a.question_key: a.answer for a in check.scalars(select(Answer))}
    assert bank == {
        "why do you want to work here": "The platform roadmap.",
        "which clouds": "AWS; GCP",
        "i agree to the privacy policy": "Yes",
    }
    answers_page = app_client.get("/answers").text
    assert "The platform roadmap." in answers_page and "salary expectation" in answers_page


def test_needs_human_can_be_finished_by_hand_then_tracked(
    app_client: TestClient, session: Session, user: User, jobs: dict[str, Job]
) -> None:
    application = _application(
        session, user, jobs["principal"], "needs_human", channel="form",
        blockers=[{"kind": "bot_check", "detail": "The form is protected by a bot check (hCaptcha), so it is yours to submit."}],
        prepared={"channel": "form", "url": "https://jobs.lever.co/acme/1/apply", "fields": [{"label": "Full name", "value": "Alex Example", "source": "profile", "required": True}], "unanswered": []},
    )  # fmt: skip
    page = app_client.get("/queue").text
    assert "Yours to finish" in page and "protected by a bot check (hCaptcha)" in page
    assert "Open the application page" in page and "Your answers, ready to enter" in page

    app_client.post(f"/applications/{application.id}/mark-submitted")
    sent = _fresh(Application, application.id)
    assert sent.status == "submitted" and sent.follow_up_at is not None

    app_client.post(
        f"/applications/{application.id}/stage",
        data={"stage": "interviewing", "note": "Panel Friday"},
    )
    app_client.post(
        f"/applications/{application.id}/notes",
        data={"notes": "Ask about on-call", "next_action": "Prep"},
    )
    moved = _fresh(Application, application.id)
    assert (
        moved.status == "interviewing"
        and moved.notes == "Ask about on-call"
        and moved.next_action == "Prep"
    )
    tracker = app_client.get("/tracker").text
    assert "Interviewing" in tracker and "Principal Platform Engineer" in tracker
    detail = app_client.get(f"/applications/{application.id}").text
    assert "What was sent" in detail and "Marked submitted" in detail and "Stage" in detail

    bad = app_client.post(
        f"/applications/{application.id}/stage",
        data={"stage": "needs_review"},
        follow_redirects=True,
    )
    assert "Unknown stage" in bad.text
    assert app_client.get("/applications/99999").status_code == 404


def test_retry_and_dismiss(
    app_client: TestClient, session: Session, user: User, jobs: dict[str, Job]
) -> None:
    application = _application(
        session,
        user,
        jobs["principal"],
        "failed",
        channel="form",
        error="The form reported: closed",
    )
    assert "Did not go through" in app_client.get("/queue").text
    app_client.post(f"/applications/{application.id}/retry")
    assert _fresh(Application, application.id).status == "preparing"
    app_client.post(f"/applications/{application.id}/dismiss", data={"reason": "Changed my mind"})
    assert _fresh(Application, application.id).status == "skipped"


def test_follow_ups_past_due_are_called_out(
    app_client: TestClient, session: Session, user: User, jobs: dict[str, Job]
) -> None:
    _application(
        session, user, jobs["principal"], "submitted",
        submitted_at=NOW - timedelta(days=30), follow_up_at=NOW - timedelta(days=20),
    )  # fmt: skip
    text = app_client.get("/tracker").text
    assert "1 application past the follow-up date" in text and "Due" in text


# -------------------------------------------------------------------- files


def test_resume_download_only_serves_files_from_the_resume_folder(
    app_client: TestClient,
    session: Session,
    user: User,
    jobs: dict[str, Job],
    settings: Settings,
    tmp_path: Path,
) -> None:
    inside = settings.resumes_dir / "job-1" / "abc" / "Alex_Example_Resume.pdf"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"%PDF-1.4 real")
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 secret")

    good = ResumeVariant(user_id=user.id, job_id=jobs["principal"].id, pdf_path=str(inside))
    evil = ResumeVariant(user_id=user.id, job_id=jobs["contract"].id, pdf_path=str(outside))
    session.add_all([good, evil])
    session.flush()
    first = _application(
        session, user, jobs["principal"], "needs_review", resume_variant_id=good.id
    )
    second = _application(
        session, user, jobs["contract"], "needs_review", resume_variant_id=evil.id
    )

    response = app_client.get(f"/applications/{first.id}/resume.pdf")
    assert response.status_code == 200 and response.content == b"%PDF-1.4 real"
    assert "Alex_Example_Resume.pdf" in response.headers["content-disposition"]
    assert app_client.get(f"/applications/{second.id}/resume.pdf").status_code == 404
    assert app_client.get(f"/applications/{first.id}/resume.docx").status_code == 404
    assert app_client.get(f"/applications/{first.id}/screenshot/0").status_code == 404


# ------------------------------------------------------------ admin pages


def test_ledger_add_and_remove(app_client: TestClient) -> None:
    assert "No submissions recorded" in app_client.get("/ledger").text
    app_client.post(
        "/ledger",
        data={
            "client_name": "Big Bank",
            "vendor_name": "TekVendor",
            "role_title": "Architect",
            "rate": "$110/hr",
        },
    )
    page = app_client.get("/ledger").text
    assert "Big Bank" in page and "TekVendor" in page and "$110/hr" in page
    with get_session_factory()() as check:
        entry = check.scalar(select(LedgerEntry))
        assert (entry.client_key, entry.vendor_key, entry.channel) == (
            "big bank",
            "tekvendor",
            "manual",
        )
    app_client.post(f"/ledger/{entry.id}/delete")
    assert "No submissions recorded" in app_client.get("/ledger").text
    assert (
        "A client name is required"
        in app_client.post("/ledger", data={"client_name": "  "}, follow_redirects=True).text
    )


def test_sources_add_pause_remove(app_client: TestClient) -> None:
    assert "No career pages yet" in app_client.get("/sources").text
    added = app_client.post(
        "/sources",
        data={"url": "https://jobs.lever.co/globex", "name": "Globex"},
        follow_redirects=True,
    )
    assert "Now watching 1 board" in added.text and "Globex" in added.text and "Lever" in added.text
    assert (
        "Already watching that board"
        in app_client.post(
            "/sources", data={"url": "jobs.lever.co/globex"}, follow_redirects=True
        ).text
    )
    with get_session_factory()() as check:
        source = check.scalar(select(Source).where(Source.kind == "lever"))
    assert "paused" in app_client.post(f"/sources/{source.id}/toggle", follow_redirects=True).text
    assert _fresh(Source, source.id).enabled is False
    app_client.post(f"/sources/{source.id}/delete")
    assert _fresh(Source, source.id) is None
    # no worker in this process: say so instead of pretending
    assert (
        "not running in this process"
        in app_client.post("/sources/check-now", follow_redirects=True).text
    )


def test_answers_edit_and_remove(app_client: TestClient) -> None:
    app_client.post("/answers", data={"question": "Do you have a non-compete?", "answer": "No"})
    with get_session_factory()() as check:
        row = check.scalar(select(Answer))
    app_client.post(f"/answers/{row.id}", data={"answer": "No, none."})
    assert _fresh(Answer, row.id).answer == "No, none."
    assert (
        "cannot be empty"
        in app_client.post(f"/answers/{row.id}", data={"answer": " "}, follow_redirects=True).text
    )
    app_client.post(f"/answers/{row.id}/delete")
    assert _fresh(Answer, row.id) is None


def test_settings_page_states_what_is_in_effect(app_client: TestClient) -> None:
    text = app_client.get("/settings").text
    assert "Nothing goes out until you approve it in the queue." in text
    assert "Not connected. Email applications are saved as drafts" in text
    assert "Northwind Systems" in text  # blocked company
    assert "Answered for United States" in text


def test_broken_config_is_explained_not_crashed(app_client: TestClient, data_dir: Path) -> None:
    (data_dir / "search.yaml").write_text("lanes: []\n")
    response = app_client.get("/feed")
    assert response.status_code == 503
    assert (
        "Your configuration needs attention" in response.text
        and "define at least one lane" in response.text
    )


def test_not_found_page(app_client: TestClient) -> None:
    response = app_client.get("/nope")
    assert response.status_code == 404 and "Nothing here" in response.text


# ----------------------------------------------------------------- security


def test_security_headers(app_client: TestClient) -> None:
    headers = app_client.get("/feed").headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert app_client.get("/static/app.css").status_code == 200
    assert app_client.get("/healthz").text == "ok"


def test_unknown_host_names_are_refused(app_client: TestClient) -> None:
    response = app_client.get("/feed", headers={"host": "evil.example"})
    assert response.status_code == 400
    assert app_client.get("/feed", headers={"host": "localhost:8000"}).status_code == 200
    assert app_client.get("/feed", headers={"host": "127.0.0.1:8000"}).status_code == 200


def test_cross_site_writes_are_refused(app_client: TestClient, jobs: dict[str, Job]) -> None:
    url = f"/jobs/{jobs['principal'].id}/apply"
    assert app_client.post(url, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert app_client.post(url, headers={"sec-fetch-site": "same-site"}).status_code == 403
    assert app_client.post(url, headers={"origin": "https://evil.example"}).status_code == 403
    assert app_client.post(url, headers={"origin": "http://testserver:9999"}).status_code == 403
    with get_session_factory()() as check:
        assert check.scalar(select(Application)) is None  # none of them got through
    assert app_client.post(url, headers={"sec-fetch-site": "same-origin"}).status_code == 303
    assert app_client.post(url, headers={"origin": "http://testserver"}).status_code == 303


def test_password_protects_everything_but_the_login_page(
    settings: Settings, session: Session, user: User
) -> None:
    settings.allowed_hosts = ["testserver"]
    settings.password = SecretStr("correct horse")
    client = TestClient(create_app(settings), follow_redirects=False)

    assert client.get("/feed").headers["location"] == "/login"
    assert client.post("/ledger", data={"client_name": "X"}).headers["location"] == "/login"
    hx = client.get("/queue", headers={"HX-Request": "true"})
    assert hx.status_code == 401 and hx.headers["hx-redirect"] == "/login"
    assert (
        client.get("/static/app.css").status_code == 200
        and client.get("/healthz").status_code == 200
    )
    assert "Sign in" in client.get("/login").text

    wrong = client.post("/login", data={"password": "nope"})
    assert wrong.status_code == 401 and "That is not the password" in wrong.text
    assert client.get("/feed").status_code == 303

    assert client.post("/login", data={"password": "correct horse"}).headers["location"] == "/feed"
    assert client.get("/feed").status_code == 200
    assert "Sign out" in client.get("/feed").text
    assert client.post("/logout").headers["location"] == "/login"
    assert client.get("/feed").status_code == 303


def _protected(settings: Settings, **client: object) -> TestClient:
    settings.allowed_hosts = ["testserver"]
    settings.password = SecretStr("correct horse")
    return TestClient(create_app(settings), follow_redirects=False, **client)  # type: ignore[arg-type]


def test_wrong_passwords_slow_sign_in_down_but_never_lock_the_owner_out(
    settings: Settings, session: Session, user: User
) -> None:
    client = _protected(settings)
    waits: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    client.app.state.login_limiter.sleep = no_sleep  # type: ignore[attr-defined]
    statuses = [client.post("/login", data={"password": "x"}).status_code for _ in range(7)]
    assert statuses == [401] * 7  # refused, each a little slower than the last
    assert waits == sorted(waits) and waits[-1] > waits[0] > 0 and max(waits) <= 5.0
    # The right password still works, from the same address, straight after.
    assert client.post("/login", data={"password": "correct horse"}).headers["location"] == "/feed"

    # Another address (as the owner behind a shared proxy would be) is delayed, not refused.
    other = TestClient(client.app, follow_redirects=False, client=("203.0.113.9", 4000))
    assert other.post("/login", data={"password": "correct horse"}).status_code == 303


def test_sign_in_throttle_stays_small_and_counts_failures_only() -> None:
    from jobportal.web.security import LOGIN_MAX_ADDRESSES, LoginLimiter

    limiter = LoginLimiter()
    for index in range(LOGIN_MAX_ADDRESSES + 500):
        limiter.failed(f"198.51.100.{index}")
    assert len(limiter._by_address) <= LOGIN_MAX_ADDRESSES
    fresh = LoginLimiter()
    assert fresh.delay("203.0.113.1") == 0.0
    fresh.succeeded("203.0.113.1")
    assert fresh.delay("203.0.113.1") == 0.0  # successes never add a delay
    fresh.failed("203.0.113.1")
    assert fresh.delay("203.0.113.1") > 0.0


def test_an_empty_password_never_signs_anyone_in(
    settings: Settings, session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _protected(settings)
    assert client.post("/login").status_code == 401
    assert client.post("/login", data={"password": ""}).status_code == 401
    assert client.get("/feed").status_code == 303

    # A blank value in the environment means "no password", not an empty one.
    monkeypatch.setenv("JOBPORTAL_PASSWORD", "   ")
    monkeypatch.setenv("JOBPORTAL_SECRET_KEY", "")
    blank = Settings()
    assert blank.password is None and blank.secret_key is None
    with pytest.raises(RuntimeError, match="without a password"):
        require_safe_binding("0.0.0.0", blank)


def test_short_passwords_and_keys_are_refused_at_start(settings: Settings) -> None:
    settings.password = SecretStr("short")
    with pytest.raises(RuntimeError, match="shorter than 8"):
        create_app(settings)
    settings.password = SecretStr("correct horse")
    settings.secret_key = SecretStr("k" * 10)
    with pytest.raises(RuntimeError, match="shorter than 32"):
        create_app(settings)


def test_the_signing_key_file_is_private_and_replaced_when_unusable(settings: Settings) -> None:
    from jobportal.web.security import session_secret

    settings.ensure_dirs()
    path = settings.data_dir / ".session-key"
    path.write_text("", encoding="utf-8")  # an empty key would let anyone forge a session
    key = session_secret(settings)
    assert len(key) >= 32 and path.read_text(encoding="utf-8") == key
    assert path.stat().st_mode & 0o077 == 0
    assert settings.data_dir.stat().st_mode & 0o077 == 0
    assert session_secret(settings) == key  # stable once written


def test_signing_out_ends_copied_sessions_too(
    settings: Settings, session: Session, user: User
) -> None:
    client = _protected(settings)
    client.post("/login", data={"password": "correct horse"})
    assert client.get("/feed").status_code == 200
    copied = dict(client.cookies)  # what someone who copied the cookie would hold

    assert client.post("/logout").headers["location"] == "/login"
    thief = TestClient(client.app, follow_redirects=False, cookies=copied)
    assert thief.get("/feed").headers["location"] == "/login"

    # A session also dies with the password it was opened under.
    client.post("/login", data={"password": "correct horse"})
    held = dict(client.cookies)
    settings.password = SecretStr("a different password")
    changed = TestClient(create_app(settings), follow_redirects=False, cookies=held)
    assert changed.get("/feed").headers["location"] == "/login"


def test_the_session_cookie_can_be_marked_secure(
    settings: Settings, session: Session, user: User
) -> None:
    settings.cookie_secure = True
    client = _protected(settings, base_url="https://testserver")
    response = client.post("/login", data={"password": "correct horse"})
    assert "secure" in response.headers["set-cookie"].lower()


def test_without_a_password_only_this_machine_is_answered(
    settings: Settings, session: Session, user: User
) -> None:
    settings.allowed_hosts = ["testserver"]
    app = create_app(settings)
    remote = TestClient(app, follow_redirects=False, client=("192.168.1.50", 40000))
    assert remote.get("/feed").status_code == 403
    assert "no password" in remote.get("/feed").text
    local = TestClient(app, follow_redirects=False, client=LOCAL)
    assert local.get("/feed").status_code == 200


def test_going_back_never_leaves_the_site(app_client: TestClient, jobs: dict[str, Job]) -> None:
    url = f"/jobs/{jobs['principal'].id}/save"
    for referer, expected in (
        ("http://testserver//evil.example/phish", None),
        ("http://testserver/\\evil.example", None),
        ("http://evil.example/feed", None),
        ("http://testserver/feed?view=all", "/feed?view=all"),
    ):
        location = app_client.post(url, headers={"referer": referer}).headers["location"]
        assert not location.startswith(("//", "/\\", "http"))
        if expected:
            assert location == expected


def test_refuses_to_listen_publicly_without_a_password(settings: Settings) -> None:
    require_safe_binding("127.0.0.1", settings)
    require_safe_binding("localhost", settings)
    with pytest.raises(RuntimeError, match="without a password"):
        require_safe_binding("0.0.0.0", settings)
    settings.password = SecretStr("x")
    with pytest.raises(RuntimeError, match="shorter than 8"):
        require_safe_binding("0.0.0.0", settings)
    settings.password = SecretStr("correct horse")
    require_safe_binding("0.0.0.0", settings)
