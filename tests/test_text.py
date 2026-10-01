from __future__ import annotations

import pytest

from jobportal.text import (
    company_key,
    find_terms,
    has_term,
    html_to_text,
    job_fingerprint,
    phrase_in_title,
    question_key,
    slugify,
    title_words,
    unescape_if_needed,
)


def test_html_to_text_keeps_paragraphs_and_bullets() -> None:
    html = (
        "<h2>About</h2><p>Build <b>platforms</b>.</p><ul><li>Kubernetes</li><li><p>Go</p></li></ul>"
    )
    assert html_to_text(html) == "About\n\nBuild platforms.\n\n- Kubernetes\n- Go"


def test_html_to_text_handles_entity_escaped_markup() -> None:
    escaped = "&lt;p&gt;Design &amp;amp; run&lt;/p&gt;&lt;ul&gt;&lt;li&gt;AWS&lt;/li&gt;&lt;/ul&gt;"
    assert unescape_if_needed(escaped).startswith("<p>")
    assert html_to_text(escaped) == "Design & run\n\n- AWS"


def test_html_to_text_drops_scripts_and_handles_plain_text() -> None:
    assert html_to_text("<p>Hi</p><script>alert(1)</script>") == "Hi"
    assert html_to_text("Tom &amp; Jerry") == "Tom & Jerry"
    assert html_to_text(None) == ""


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Acme, Inc.", "ACME"),
        ("The Acme Corporation", "acme"),
        ("Acme Holdings LLC", "Acme"),
    ],
)
def test_company_key_ignores_legal_suffixes(left: str, right: str) -> None:
    assert company_key(left) == company_key(right)


def test_company_key_keeps_distinct_companies_apart() -> None:
    assert company_key("Acme Robotics") != company_key("Acme Bank")


def test_title_words_expand_abbreviations() -> None:
    assert title_words("Sr. Platform Eng. (K8s)") == [
        "senior",
        "platform",
        "engineer",
        "kubernetes",
    ]
    assert title_words("Vice President, Infrastructure") == ["vp", "infrastructure"]
    assert title_words("Head of Platform") == ["head", "platform"]


def test_phrase_in_title_is_order_free_and_whole_word() -> None:
    words = title_words("Principal Engineer, Cloud Platform")
    assert phrase_in_title("platform engineer", words)
    assert phrase_in_title("principal cloud engineer", words)
    assert not phrase_in_title("platform architect", words)
    # "engineer" must not match "engineering"
    assert not phrase_in_title("platform engineer", title_words("Platform Engineering Recruiter"))


def test_fingerprint_same_role_across_locations() -> None:
    assert job_fingerprint("Acme, Inc.", "Sr. Platform Engineer") == job_fingerprint(
        "ACME", "Senior Platform Engineer"
    )
    assert job_fingerprint("Acme", "Platform Engineer") != job_fingerprint("Acme", "Data Engineer")


@pytest.mark.parametrize(
    ("text", "term", "expected"),
    [
        ("Experience with Kubernetes and Helm", "kubernetes", True),
        ("We run k8s everywhere", "Kubernetes", True),  # alias
        ("Deep Amazon Web Services knowledge", "AWS", True),  # alias, reversed
        ("CI/CD pipelines", "ci/cd", True),
        ("continuous delivery pipelines", "CI/CD", True),
        ("C++ and Rust", "C++", True),
        ("Excellent communication", "C", False),
        ("Strong Go and Python", "Go", True),
        ("Our go-to-market team", "Go", False),
        ("Go to market strategy", "Go", False),
        ("golang services", "Go", True),
        ("R&D organisation", "R", False),
        ("javascript", "Java", False),  # whole term only
        ("Terraform, Ansible", "terraform", True),
        ("zero-trust networking", "Zero trust", True),
        ("", "AWS", False),
    ],
)
def test_has_term(text: str, term: str, expected: bool) -> None:
    assert has_term(text, term) is expected


def test_find_terms_dedupes_aliases_and_keeps_order() -> None:
    text = "Kubernetes (k8s), AWS, Terraform"
    assert find_terms(text, ["Terraform", "k8s", "Kubernetes", "GCP", "AWS"]) == [
        "Terraform",
        "k8s",
        "AWS",
    ]


def test_question_key_normalises_punctuation_and_markers() -> None:
    assert question_key("Are you legally authorized to work in the U.S.? *") == question_key(
        "are you legally authorized to work in the u s"
    )
    assert question_key("LinkedIn Profile (optional)") == "linkedin profile"


def test_slugify() -> None:
    assert slugify("Principal Engineer, Platform (Remote)") == "principal-engineer-platform-remote"
    assert slugify("") == "untitled"
