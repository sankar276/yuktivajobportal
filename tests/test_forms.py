from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Browser
from sqlalchemy.orm import Session

from jobportal.apply.answers import AnswerBook, load_answers, save_answer
from jobportal.apply.forms.fields import FieldKind, FormField, classify, match_option
from jobportal.apply.forms.filler import (
    FormUrlRefused,
    assist,
    build_plan,
    check_form_url,
    prepare,
    scan,
    submit,
)
from jobportal.config import Profile, UserConfig
from jobportal.http import PoliteClient
from jobportal.models import User
from jobportal.settings import Settings
from tests.formserver import FormServer


def _field(
    label: str,
    type_: str = "text",
    options: list[str] | None = None,
    name: str = "",
    required=False,
):
    return FormField(
        ref="#x",
        label=label,
        type=type_,
        name=name,
        required=required,
        options=[{"label": text, "ref": str(index)} for index, text in enumerate(options or [])],
    )


@pytest.fixture
def profile(user_config: UserConfig) -> Profile:
    return user_config.profile


@pytest.fixture
def resume(tmp_path: Path) -> Path:
    path = tmp_path / "Alex_Example_Resume.pdf"
    path.write_bytes(b"%PDF-1.4 test resume")
    return path


@pytest.fixture
def book(profile: Profile, resume: Path) -> AnswerBook:
    return AnswerBook(profile, {}, resume)


# ----------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("label", "type_", "name", "kind"),
    [
        ("First Name", "text", "", FieldKind.first_name),
        ("Given name", "text", "", FieldKind.first_name),
        ("Last Name", "text", "", FieldKind.last_name),
        ("Full name", "text", "", FieldKind.full_name),
        ("Name", "text", "", FieldKind.full_name),
        ("Legal name", "text", "", FieldKind.full_name),
        ("Preferred first name", "text", "", FieldKind.preferred_name),
        ("Email", "text", "", FieldKind.email),
        ("", "email", "", FieldKind.email),
        ("Phone number", "text", "", FieldKind.phone),
        ("Mobile", "tel", "", FieldKind.phone),
        ("LinkedIn Profile", "text", "", FieldKind.linkedin),
        ("LinkedIn URL", "url", "", FieldKind.linkedin),
        ("GitHub", "text", "", FieldKind.github),
        ("Website or portfolio", "text", "", FieldKind.website),
        ("Current company", "text", "", FieldKind.current_company),
        ("Company", "text", "", FieldKind.current_company),
        ("", "text", "org", FieldKind.current_company),
        ("Current title", "text", "", FieldKind.current_title),
        ("Location (City)", "text", "", FieldKind.location),
        ("City", "text", "", FieldKind.city),
        ("Zip code", "text", "", FieldKind.postal_code),
        ("Resume/CV", "file", "", FieldKind.resume),
        ("", "file", "resume", FieldKind.resume),
        ("Cover Letter", "file", "", FieldKind.cover_letter),
        ("Years of experience", "number", "", FieldKind.years_experience),
        # Mentions a field word, but is a real question.
        (
            "What excites you about our company and this role in particular? Tell us in a few lines.",
            "textarea",
            "",
            FieldKind.question,
        ),
        ("Why do you want to work here?", "textarea", "", FieldKind.question),
        ("Transcript", "file", "transcript", FieldKind.question),
    ],
)
def test_classify_common_fields(
    profile: Profile, label: str, type_: str, name: str, kind: FieldKind
) -> None:
    assert classify(_field(label, type_, name=name), profile) is kind


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Are you legally authorized to work in the United States?", FieldKind.work_authorized),
        ("Are you authorized to work in the US?", FieldKind.work_authorized),
        ("Are you eligible to work in the U.S.?", FieldKind.work_authorized),
        (
            "Will you now or in the future require sponsorship for employment visa status in the United States?",
            FieldKind.needs_sponsorship,
        ),
        ("Do you require visa sponsorship to work in the US?", FieldKind.needs_sponsorship),
        # Anything not in the plain, standard wording is left for the person:
        ("Are you authorized to work in the US without sponsorship?", FieldKind.question),
        ("Are you legally authorized to work in Canada?", FieldKind.question),
        (
            "Are you legally authorized to work in the country where this job is located?",
            FieldKind.question,
        ),
        ("Are you legally authorized to work?", FieldKind.question),
        (
            "Can you work in the US without requiring sponsorship now or in the future?",
            FieldKind.question,
        ),
        ("Do you not require sponsorship in the United States?", FieldKind.question),
        ("What is your current visa status?", FieldKind.question),
    ],
)
def test_legal_questions_are_only_recognised_in_standard_wording(
    profile: Profile, label: str, kind: FieldKind
) -> None:
    assert classify(_field(label, "select", ["Yes", "No"]), profile) is kind


def test_eeo_fields_are_recognised_only_as_choices(profile: Profile) -> None:
    options = ["Male", "Female", "Decline To Self Identify"]
    assert classify(_field("Gender", "select", options), profile) is FieldKind.eeo_gender
    assert classify(_field("Race / Ethnicity", "select", options), profile) is FieldKind.eeo_race
    assert classify(_field("Veteran Status", "select", options), profile) is FieldKind.eeo_veteran
    assert (
        classify(_field("Disability Status", "radio", options), profile) is FieldKind.eeo_disability
    )
    assert (
        classify(_field("Tell us about gender diversity initiatives you led", "textarea"), profile)
        is FieldKind.question
    )


@pytest.mark.parametrize(
    ("answer", "options", "expected"),
    [
        ("Yes", ["Yes", "No"], "Yes"),
        ("No", ["Yes", "No"], "No"),
        (
            "no",
            ["Yes, I will require sponsorship", "No, I will not require sponsorship"],
            "No, I will not require sponsorship",
        ),
        (
            "Yes",
            ["Yes - US citizen", "Yes - permanent resident", "No"],
            None,
        ),  # ambiguous: never guess
        ("decline", ["Male", "Female", "Decline To Self Identify"], "Decline To Self Identify"),
        ("decline", ["Yes", "No", "I don't wish to answer"], "I don't wish to answer"),
        ("decline", ["Yes", "No", "I do not want to answer"], "I do not want to answer"),
        ("decline", ["Male", "Female"], None),
        (
            "Company careers page",
            ["LinkedIn", "Company careers page", "Referral"],
            "Company careers page",
        ),
        ("Referral", ["LinkedIn", "Employee referral", "Other"], "Employee referral"),
        ("Maybe", ["Yes", "No"], None),
        ("", ["Yes", "No"], None),
    ],
)
def test_match_option(answer: str, options: list[str], expected: str | None) -> None:
    picked = match_option(answer, [{"label": o, "ref": str(i)} for i, o in enumerate(options)])
    assert (picked["label"] if picked else None) == expected


# --------------------------------------------------------------- answer book


def test_answer_book_sources_in_order(profile: Profile, resume: Path) -> None:
    question = "What is your notice period?"
    from jobportal.text import question_key

    book = AnswerBook(profile, {question_key(question): "Four weeks"}, resume)
    stored = book.resolve(_field(question))
    assert stored is not None and (stored.value, stored.source) == ("Four weeks", "answer bank")
    standard = AnswerBook(profile, {}, resume).resolve(_field(question))
    assert standard is not None and (standard.value, standard.source) == (
        "2 weeks",
        "standard answer",
    )
    name = book.resolve(_field("First Name"))
    assert name is not None and (name.value, name.source) == ("Alex", "profile")
    assert book.resolve(_field("What is your favourite database?")) is None


def test_unset_work_authorization_is_never_guessed(profile: Profile, resume: Path) -> None:
    unset = profile.model_copy(deep=True)
    unset.work_authorization.authorized = None
    unset.work_authorization.needs_sponsorship = None
    book = AnswerBook(unset, {}, resume)
    question = _field(
        "Are you legally authorized to work in the United States?", "select", ["Yes", "No"]
    )
    assert book.kind_of(question) is FieldKind.work_authorized
    assert book.resolve(question) is None


def test_eeo_defaults_to_the_decline_option_and_is_left_blank_without_one(book: AnswerBook) -> None:
    with_decline = _field("Gender", "select", ["Male", "Female", "Decline To Self Identify"])
    picked = book.resolve(with_decline)
    assert picked is not None and picked.value == "Decline To Self Identify"
    assert book.resolve(_field("Gender", "select", ["Male", "Female"])) is None


def test_answer_bank_roundtrip(session: Session, user: User) -> None:
    save_answer(
        session, user.id, "Why do you want to work here? *", "Because of the platform work."
    )
    save_answer(session, user.id, "why do you want to work here", "Updated answer.")
    assert save_answer(session, user.id, "Blank?", "   ") is None
    assert load_answers(session, user.id) == {"why do you want to work here": "Updated answer."}


# ----------------------------------------------------------------- url guard


def test_form_urls_must_be_public_https(settings: Settings) -> None:
    check_form_url("https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1", settings)
    for bad in (
        "http://jobs.example.com/apply",
        "https://localhost/apply",
        "http://127.0.0.1:8000/x",
        "https://10.0.0.5/apply",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "not a url",
    ):
        with pytest.raises(FormUrlRefused):
            check_form_url(bad, settings)
    settings.allow_local_forms = True
    check_form_url("http://127.0.0.1:8000/x", settings)


# ------------------------------------------------------------------ browser

pytestmark_browser = pytest.mark.browser


@pytest.mark.browser
def test_scan_reads_labels_types_and_ignores_hidden_fields(
    browser: Browser, form_server: FormServer
) -> None:
    page = browser.new_page()
    try:
        page.goto(form_server.url("classic.html"))
        scanned = scan(page)
    finally:
        page.close()
    by_label = {f["label"]: f for f in scanned["fields"]}
    assert list(by_label)[:5] == ["First Name", "Last Name", "Email", "Phone", "Resume/CV"]
    assert by_label["First Name"]["required"] and not by_label["Phone"]["required"]
    assert by_label["Resume/CV"]["type"] == "file"
    assert by_label["Gender"]["type"] == "select"
    assert [o["label"] for o in by_label["Gender"]["options"]] == [
        "Male",
        "Female",
        "Decline To Self Identify",
    ]
    names = {f["name"] for f in scanned["fields"]}
    assert "fingerprint" not in names and "website_url_confirm" not in names
    assert scanned["captcha"] is None and scanned["login"] is False
    assert scanned["submitText"] == "Submit Application"


@pytest.mark.browser
def test_prepare_plans_every_answer_without_touching_the_form(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    outcome = prepare(browser, form_server.url("classic.html"), book, settings=settings)
    assert outcome.status == "ready" and outcome.blockers == []
    plan = outcome.plan
    assert plan is not None
    planned = {p.field.label: (p.resolution.value, p.resolution.source) for p in plan.fill}
    assert planned["First Name"] == ("Alex", "profile")
    assert planned["Last Name"] == ("Example", "profile")
    assert planned["Email"] == ("alex@example.com", "profile")
    assert planned["LinkedIn Profile"] == ("https://www.linkedin.com/in/alex-example", "profile")
    assert planned["Are you legally authorized to work in the United States?"] == ("Yes", "profile")
    assert planned[
        "Will you now or in the future require sponsorship for employment visa status in the United States?"
    ] == ("No", "profile")
    assert planned["What are your salary expectations?"][1] == "standard answer"
    assert planned["Gender"][0] == "Decline To Self Identify"
    assert planned["Veteran Status"][0] == "I don't wish to answer"
    assert planned["Disability Status"][0] == "I do not want to answer"
    assert [f.label for f in plan.left_blank] == ["Cover Letter"]
    assert form_server.posts() == []  # nothing sent
    prepared = plan.to_prepared()
    assert {"label": "Resume/CV", "value": "Alex_Example_Resume.pdf"}.items() <= next(
        f for f in prepared["fields"] if f["label"] == "Resume/CV"
    ).items()


@pytest.mark.browser
def test_submit_sends_exactly_the_planned_answers(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings, tmp_path: Path
) -> None:
    outcome = submit(
        browser,
        form_server.url("classic.html"),
        book,
        settings=settings,
        screenshot_dir=tmp_path / "shots",
        label="app-1",
    )
    assert outcome.status == "submitted", outcome.error
    assert "Thank you for applying" in outcome.confirmation
    (sent,) = form_server.posts()
    assert sent.first("job_application[first_name]") == "Alex"
    assert sent.first("job_application[last_name]") == "Example"
    assert sent.first("job_application[email]") == "alex@example.com"
    assert sent.first("job_application[answers][1]") == "1"  # authorized: Yes
    assert sent.first("job_application[answers][2]") == "0"  # sponsorship: No
    assert sent.first("job_application[gender]") == "3"  # decline
    assert sent.first("job_application[race]") == "8"
    assert sent.files["job_application[resume]"] == ("Alex_Example_Resume.pdf", 20)
    assert "job_application[cover_letter]" not in sent.files
    assert sent.first("website_url_confirm") == ""  # the hidden field stays empty
    assert [Path(s).name for s in outcome.screenshots] == [
        "app-1-filled.png",
        "app-1-after-submit.png",
    ]
    assert all(Path(s).exists() for s in outcome.screenshots)


@pytest.mark.browser
def test_unanswered_required_questions_stop_the_submission(
    browser: Browser, form_server: FormServer, profile: Profile, resume: Path, settings: Settings
) -> None:
    url = form_server.url("custom_questions.html")
    book = AnswerBook(profile, {}, resume)

    outcome = submit(browser, url, book, settings=settings)
    assert outcome.status == "needs_answers"
    assert [f.label for f in outcome.plan.unanswered] == [
        "Why do you want to work at Acme Robotics?",
        "I have read and agree to the candidate privacy policy",
    ]
    assert form_server.posts() == []  # refused before anything was typed or sent

    answered = AnswerBook(
        profile,
        {
            f.key: answer
            for f, answer in zip(
                outcome.plan.unanswered, ["The platform roadmap.", "Yes"], strict=True
            )
        },
        resume,
    )
    done = submit(browser, url, answered, settings=settings)
    assert done.status == "submitted", done.error
    (sent,) = form_server.posts()
    assert sent.first("job_application[answers][9]") == "The platform roadmap."
    assert sent.first("consent") == "1"


@pytest.mark.browser
def test_a_bot_check_hands_the_form_to_the_person(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    url = form_server.url("captcha.html")
    prepared = prepare(browser, url, book, settings=settings)
    assert prepared.status == "needs_human"
    assert (
        prepared.blockers[0]["kind"] == "bot_check" and "hCaptcha" in prepared.blockers[0]["detail"]
    )
    assert len(prepared.plan.fill) > 5  # the answers are still worked out for the hand-off

    refused = submit(browser, url, book, settings=settings)
    assert refused.status == "needs_human"
    assert form_server.posts() == []


@pytest.mark.browser
def test_login_wall_and_multi_step_forms_need_a_person(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    login = submit(browser, form_server.url("login.html"), book, settings=settings)
    assert login.status == "needs_human"
    assert {"login"} <= {b["kind"] for b in login.blockers}
    steps = submit(browser, form_server.url("multistep.html"), book, settings=settings)
    assert steps.status == "needs_human"
    assert [b["kind"] for b in steps.blockers] == ["no_submit"]
    assert form_server.posts() == []


@pytest.mark.browser
def test_radio_and_checkbox_groups(
    browser: Browser, form_server: FormServer, profile: Profile, resume: Path, settings: Settings
) -> None:
    url = form_server.url("lever.html")
    first = prepare(browser, url, AnswerBook(profile, {}, resume), settings=settings)
    assert first.status == "needs_answers"
    (question,) = first.plan.unanswered
    assert question.label == "Are you open to working Central Time hours?"
    assert question.type == "radio" and question.option_labels == ["Yes", "No"]
    clouds = next(f for f in first.plan.left_blank if "clouds" in f.label)
    assert clouds.type == "checkbox" and clouds.option_labels == ["AWS", "Azure", "GCP"]

    book = AnswerBook(profile, {question.key: "Yes", clouds.key: "AWS; GCP"}, resume)
    outcome = submit(browser, url, book, settings=settings)
    assert outcome.status == "submitted", outcome.error
    (sent,) = form_server.posts()
    assert sent.first("name") == "Alex Example"
    assert sent.first("org") == "Northwind Systems"
    assert sent.first("urls[LinkedIn]") == "https://www.linkedin.com/in/alex-example"
    assert sent.first("urls[GitHub]") == "https://github.com/alex-example"
    assert sent.first("cards[11][field0]") == "Yes"
    assert sent.fields["cards[11][field1]"] == ["AWS", "GCP"]
    assert sent.files["resume"][0] == "Alex_Example_Resume.pdf"
    assert sent.first("comments") == ""


@pytest.mark.browser
def test_single_page_form_with_custom_controls(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    outcome = submit(browser, form_server.url("react.html"), book, settings=settings)
    assert outcome.status == "submitted", outcome.error or outcome.blockers
    assert "received your application" in outcome.confirmation
    (sent,) = form_server.posts()
    assert sent.first("name") == "Alex Example"
    assert sent.first("auth") == "yes"  # visually hidden radio
    assert sent.first("location") == "Austin, TX"  # chosen from the suggestion list
    assert sent.first("years") == "15"
    assert sent.files["resume"][0] == "Alex_Example_Resume.pdf"


@pytest.mark.browser
def test_combobox_without_a_matching_option_is_not_forced(
    browser: Browser, form_server: FormServer, profile: Profile, resume: Path, settings: Settings
) -> None:
    elsewhere = profile.model_copy(deep=True)
    elsewhere.location.city, elsewhere.location.region = "Reykjavik", ""
    outcome = submit(
        browser, form_server.url("react.html"), AnswerBook(elsewhere, {}, resume), settings=settings
    )
    assert outcome.status == "needs_human"
    assert "Could not fill" in outcome.blockers[0]["detail"]
    assert form_server.posts() == []


@pytest.mark.browser
def test_no_confirmation_is_never_reported_as_submitted(
    browser: Browser,
    form_server: FormServer,
    book: AnswerBook,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jobportal.apply.forms.filler.OUTCOME_TIMEOUT_S", 2.0)
    outcome = submit(browser, form_server.url("silent.html"), book, settings=settings)
    assert outcome.status == "failed"
    assert "did not confirm" in outcome.error
    assert len(form_server.posts()) == 1  # it was sent once, and is flagged for a human check


@pytest.mark.browser
def test_form_errors_are_reported(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    outcome = submit(browser, form_server.url("rejects.html"), book, settings=settings)
    assert outcome.status == "failed"
    assert "no longer accepting applications" in outcome.error


@pytest.mark.browser
def test_robots_txt_is_honoured_before_opening_the_page(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    form_server.robots = "User-agent: *\nDisallow: /classic.html\n"
    with PoliteClient(settings) as client:
        outcome = prepare(
            browser, form_server.url("classic.html"), book, settings=settings, client=client
        )
        assert outcome.status == "needs_human" and outcome.blockers[0]["kind"] == "robots"
        assert ("GET", "/classic.html") not in form_server.requests
        allowed = prepare(
            browser, form_server.url("lever.html"), book, settings=settings, client=client
        )
    assert allowed.status == "needs_answers"


@pytest.mark.browser
def test_assist_fills_but_never_submits(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    outcome = assist(
        browser, form_server.url("captcha.html"), book, settings=settings, wait_seconds=1.5
    )
    assert outcome.status == "needs_human"
    assert outcome.blockers[-1]["kind"] == "not_confirmed"
    assert form_server.posts() == []


@pytest.mark.browser
def test_assist_records_a_submission_made_by_the_person(
    browser: Browser, form_server: FormServer, book: AnswerBook, settings: Settings
) -> None:
    context = browser.new_context()
    # Stand-in for the person: clicks submit one second after the page is ready.
    context.add_init_script(
        "window.addEventListener('load', () => setTimeout(() => {"
        " const b = document.querySelector('#submit_app'); if (b) b.click(); }, 1000));"
    )

    class OneContext:
        def new_page(self):
            return context.new_page()

    try:
        outcome = assist(
            OneContext(), form_server.url("classic.html"), book, settings=settings, wait_seconds=15
        )
    finally:
        context.close()
    assert outcome.status == "submitted", outcome.blockers
    (sent,) = form_server.posts()
    assert sent.first("job_application[first_name]") == "Alex"  # filled before the person clicked


def test_build_plan_reports_missing_form(book: AnswerBook) -> None:
    plan = build_plan({"url": "https://x", "fields": [], "submit": None}, book)
    assert plan.status == "needs_human"
    assert [b["kind"] for b in plan.blockers] == ["no_form"]
