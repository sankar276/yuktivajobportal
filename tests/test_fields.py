"""What goes into a form field on your behalf: the cases a reviewer broke."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobportal.apply.answers import AnswerBook
from jobportal.apply.forms.fields import FieldKind, FormField, classify, standard_answer
from jobportal.config import Profile, UserConfig
from jobportal.text import question_key

YES_NO = ["Yes", "No"]


def field(
    label: str, type_: str = "text", options: list[str] | None = None, name: str = ""
) -> FormField:
    return FormField(
        ref="#x",
        label=label,
        type=type_,
        name=name,
        options=[{"label": text, "ref": str(index)} for index, text in enumerate(options or [])],
    )


@pytest.fixture
def profile(user_config: UserConfig) -> Profile:
    return user_config.profile  # authorized: true, needs_sponsorship: false, country: United States


@pytest.fixture
def book(profile: Profile, tmp_path: Path) -> AnswerBook:
    resume = tmp_path / "Alex_Example_Resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    return AnswerBook(profile, {}, resume)


# ------------------------------------------------------------------- legal


@pytest.mark.parametrize(
    ("label", "answer"),
    [
        ("Are you legally authorized to work in the United States?", "Yes"),
        ("Are you authorized to work in the US?", "Yes"),
        ("Are you eligible to work in the U.S.?", "Yes"),
        ("Do you have the legal right to work in the United States of America?", "Yes"),
        ("Do you require visa sponsorship to work in the US?", "No"),
        (
            "Will you now or in the future require sponsorship for employment visa status "
            "(e.g., H-1B visa status) in the United States?",
            "No",
        ),
        ("Do you now or will you in the future require sponsorship to work in the USA?", "No"),
    ],
)
def test_standard_legal_wording_is_answered_from_the_profile(
    book: AnswerBook, label: str, answer: str
) -> None:
    picked = book.resolve(field(label, "select", YES_NO))
    assert picked is not None and (picked.value, picked.source) == (answer, "profile")


@pytest.mark.parametrize(
    "label",
    [
        # inverted or conditional wording
        "Are you no longer eligible to work in the United States?",
        "Is there any reason you would be prevented from being legally permitted to work in the United States?",
        "Are you currently restricted from being authorized to work in the United States?",
        "Has your right to be legally authorized to work in the United States expired or been revoked?",
        "Are you only authorized to work in the United States for your current employer?",
        "Are you authorized to work in the United States solely on a temporary visa such as OPT?",
        "No sponsorship is needed for me to work in the United States",
        "Sponsorship is never needed for me in the US",
        "Can you work in the US, with no need for a sponsor?",
        "Are you authorized to work in the United States (without sponsorship)?",
        "Are you authorized to work in the US without sponsorship?",
        # another place, a code, or no place at all
        "Are you legally authorized to work in CA?",
        "Are you legally authorized to work in DE?",
        "Are you legally authorized to work in IN?",
        "Are you authorized to work in Georgia?",
        "ARE YOU LEGALLY AUTHORIZED TO WORK IN CANADA OR MEXICO?",
        "Are you legally authorized to work in the United States and in Luxembourg?",
        "Do you require visa sponsorship to work in Qatar (our US team will ask)?",
        "Are you legally authorized to work?",
        "Will you now or in the future require sponsorship for employment visa status?",
        "What is your current visa status?",
    ],
)
def test_unusual_legal_wording_is_never_answered_for_you(book: AnswerBook, label: str) -> None:
    question = field(label, "select", YES_NO)
    assert book.kind_of(question) is FieldKind.question
    assert book.resolve(question) is None


@pytest.mark.parametrize(
    ("label", "options"),
    [
        (
            "Do you require visa sponsorship to work in the US?",
            ["Yes", "No, I am a U.S. citizen or permanent resident"],
        ),
        (
            "Are you legally authorized to work in the United States?",
            ["Yes, but I will require sponsorship", "No"],
        ),
        (
            "Are you legally authorized to work in the United States?",
            ["Yes - I hold a green card", "No"],
        ),
        (
            "Do you require visa sponsorship to work in the US?",
            ["Yes", "No - I am a US citizen", "Not sure"],
        ),
    ],
)
def test_legal_answers_only_pick_a_bare_yes_or_no(
    book: AnswerBook, label: str, options: list[str]
) -> None:
    assert book.resolve(field(label, "radio", options)) is None


def test_a_legal_question_is_never_typed_into_a_text_box(
    book: AnswerBook, profile: Profile
) -> None:
    question = field("Are you legally authorized to work in the United States?", "text")
    assert book.kind_of(question) is FieldKind.work_authorized
    assert book.resolve(question) is None
    # What you answered yourself for that exact question is yours to reuse.
    answered = AnswerBook(profile, {question_key(question.label): "Yes, as a citizen"}, None)
    picked = answered.resolve(question)
    assert picked is not None and picked.source == "answer bank"


def test_another_country_profile_uses_its_own_country(profile: Profile) -> None:
    canadian = profile.model_copy(deep=True)
    canadian.work_authorization.country = "Canada"
    book = AnswerBook(canadian, {}, None)
    assert book.resolve(field("Are you legally authorized to work in Canada?", "select", YES_NO))
    assert (
        book.resolve(
            field("Are you legally authorized to work in the United States?", "select", YES_NO)
        )
        is None
    )


# ------------------------------------------------------- your details only


@pytest.mark.parametrize(
    ("label", "type_", "name"),
    [
        ("How many years of experience do you have with Rust?", "number", ""),
        ("Years of experience with SAP", "number", ""),
        ("Years of relevant experience", "number", ""),
        ("Years of experience managing teams of 50+", "select", ""),
        ("Referrer's email", "email", "referrer_email"),
        ("Email of the employee who referred you", "email", "email"),
        ("Emergency contact phone", "tel", "phone"),
        ("Reference's full name", "text", "name"),
        ("Hiring manager's first name", "text", ""),
        ("Phone extension", "tel", ""),
        ("City of birth", "text", ""),
        ("Which city are you applying for?", "text", ""),
        ("Link to resume", "text", "resume"),
        ("Paste your resume", "textarea", ""),
        ("Resume/CV URL", "url", ""),
        ("Cover letter", "textarea", ""),
    ],
)
def test_fields_that_are_not_yours_to_fill_are_questions(
    book: AnswerBook, label: str, type_: str, name: str
) -> None:
    question = field(label, type_, name=name)
    assert book.kind_of(question) is FieldKind.question
    assert book.resolve(question) is None  # in particular: never the resume's path on disk


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Years of experience", FieldKind.years_experience),
        ("Total years of professional experience", FieldKind.years_experience),
        ("How many years of work experience do you have?", FieldKind.years_experience),
        ("Email address", FieldKind.email),
        ("Your e-mail", FieldKind.email),
        ("Phone number", FieldKind.phone),
        ("LinkedIn URL", FieldKind.linkedin),
        ("GitHub URL", FieldKind.github),
        ("Portfolio URL", FieldKind.website),
        ("Current employer", FieldKind.current_company),
        ("State / Province", FieldKind.region),
        ("Country of residence", FieldKind.country),
    ],
)
def test_plain_labels_still_map_to_your_details(
    profile: Profile, label: str, kind: FieldKind
) -> None:
    assert classify(field(label), profile) is kind


# --------------------------------------------------------------------- eeo


@pytest.mark.parametrize(
    ("label", "options"),
    [
        ("Do you consent to a disability-related medical exam after an offer?", ["I consent", "I decline"]),
        ("Do you require an accommodation due to a disability?", YES_NO),
        ("We give hiring preference to veterans. Do you want to be considered?", YES_NO),
        ("Experience working with veteran communities?", YES_NO),
        ("Are you willing to work with our gender-diverse leadership team on weekends?", YES_NO),
    ],
)  # fmt: skip
def test_questions_that_only_mention_an_eeo_word_are_not_self_identification(
    book: AnswerBook, label: str, options: list[str]
) -> None:
    question = field(label, "radio", options)
    assert book.kind_of(question) is FieldKind.question
    assert book.resolve(question) is None


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Gender", FieldKind.eeo_gender),
        ("What is your gender identity?", FieldKind.eeo_gender),
        ("Race/Ethnicity", FieldKind.eeo_race),
        ("Are you Hispanic/Latino?", FieldKind.eeo_race),
        ("Veteran Status", FieldKind.eeo_veteran),
        ("Disability Status", FieldKind.eeo_disability),
        ("Do you have a disability?", FieldKind.eeo_disability),
    ],
)
def test_self_identification_questions_are_recognised(
    profile: Profile, label: str, kind: FieldKind
) -> None:
    options = ["Yes", "No", "I don't wish to answer"]
    assert classify(field(label, "select", options), profile) is kind


# -------------------------------------------------------- standard answers


@pytest.mark.parametrize(
    "label",
    [
        "Are you currently located in or willing to relocate to Austin, TX?",
        "If you are not willing to relocate, are you able to commute daily?",
        "Do you have a non-compete or notice period that prevents you from starting?",
        "Are you willing to relocate if you need visa sponsorship?",
    ],
)
def test_standard_answers_skip_compound_and_legal_questions(profile: Profile, label: str) -> None:
    assert standard_answer(label, profile) is None


def test_standard_answers_still_cover_simple_questions(profile: Profile) -> None:
    assert standard_answer("What is your notice period?", profile) == "2 weeks"
    assert standard_answer("Are you willing to relocate?", profile) is not None
