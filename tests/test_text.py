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
    states,
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


@pytest.mark.parametrize(
    ("title", "words"),
    [
        ("Sr.Staff Platform Engineer", ["senior", "staff", "platform", "engineer"]),
        ("Staff+ Platform Engineer", ["staff", "platform", "engineer"]),
        ("V.P. Platform Engineering", ["vp", "platform", "engineering"]),
        ("Vice-President, Platform", ["vp", "platform"]),
        ("Senior Architect\u2013Cloud Platform", ["senior", "architect", "cloud", "platform"]),
        ("Lead DevOps/SRE", ["lead", "devops", "sre"]),
        ("Cloud Architect, Assistant Vice President", ["cloud", "architect", "avp"]),
        ("Asst. Vice President, Cloud", ["avp", "cloud"]),
        ("Senior Vice President, Engineering", ["svp", "engineering"]),
        ("Executive Vice President", ["evp"]),
        # Names written with a symbol stay whole.
        ("Senior .NET Developer", ["senior", ".net", "developer"]),
        ("Node.js Developer", ["node.js", "developer"]),
        ("C++ Engineer", ["c++", "engineer"]),
        ("C# Developer", ["c#", "developer"]),
    ],
)
def test_title_words_split_however_the_title_is_written(title: str, words: list[str]) -> None:
    assert title_words(title) == words


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
        # "Go", "R" and "C" are also words and letters: they count where a skill stands.
        ("Go above and beyond", "Go", False),
        ("Let's Go!", "Go", False),
        ("Go-live support", "Go", False),
        ("Go/No-Go decisions", "Go", False),
        ("We go the extra mile", "Go", False),
        ("ready to go, willing to learn", "Go", False),
        ("Python, GO, Rust", "Go", True),
        ("python, go, rust", "Go", True),  # lower case only inside a list
        ("Operators in Go.", "Go", True),
        ("Experience with Go", "Golang", True),
        ("Toys R Us", "R", False),
        ("R & D", "R", False),
        ("Python, R, SQL", "R", True),
        ("Statistics in R", "R", True),
        ("Series C funding", "C", False),
        ("Objective-C", "C", False),
        ("C-suite stakeholders", "C", False),
        ("C, C++ and Rust", "C", True),
        ("a swift response", "Swift", False),
        ("iOS (Swift)", "Swift", True),
        ("experience with go and python", "Go", True),
        ("experience with rust", "Rust", True),
        ("Python/Go/Rust", "Go", True),
        ("Ready, Set, Go! Join us", "Go", False),
        ("C/C++", "C", True),
        ("A/R and A/P processing", "R", False),
        # Tool names that are also English words.
        ("Harness the power of AI", "Harness", False),
        ("We harness the power of data", "Harness", False),
        ("- Harness the power of data to drive decisions", "Harness", False),
        ("Harness pipelines", "Harness", True),
        ("CI/CD with Harness or Spinnaker", "Harness", True),
        ("Deploying Helm charts to EKS", "Helm", True),
        ("Helm charts", "Helm", True),
        ("experience with helm, kustomize", "Helm", True),
        ("at the helm of our platform", "Helm", False),
        ("everything is in a state of flux", "Flux", False),
        ("Flux CD", "Flux", True),
        ("Spark innovation across teams", "Spark", False),
        ("will spark new ideas", "Spark", False),
        ("Experience with Apache Spark", "Spark", True),
        ("based in Hong Kong", "Kong", False),
        ("Kong API gateway", "Kong", True),
        ("Head Chef wanted", "Chef", False),
        ("Chef, Puppet or Ansible", "Puppet", True),
        ("Offices in Salt Lake City", "Salt", False),
        ("a digital nomad lifestyle", "Nomad", False),
        ("Zero trust identity with Vault.", "Vault", True),
        ("hashicorp vault", "Vault", True),
        # Not inside an address.
        ("Send email to hr@example.ai", "AI", False),
        ("AI/ML platform", "AI", True),
        ("CI and CD pipelines", "CI/CD", True),
        # Versions, symbols and joined spellings.
        ("C++17 and later", "C++", True),
        ("Java 21", "Java", True),
        ("Python3", "Python", True),
        ("ASP.NET Core", ".NET", True),
        ("identity & access management", "identity and access management", True),
        ("SOC2 compliance", "SOC 2", True),
        ("NoSQL stores", "SQL", False),
    ],
)
def test_has_term(text: str, term: str, expected: bool) -> None:
    assert has_term(text, term) is expected


@pytest.mark.parametrize(
    ("text", "phrase", "expected"),
    [
        ("Relocation required", "relocation required", True),
        ("Relocation is required.", "relocation required", True),
        ("Relocation will be required", "relocation required", True),
        ("No relocation required.", "relocation required", False),
        ("Relocation is not required", "relocation required", False),
        ("There isn't any relocation required", "relocation required", False),
        ("no travel or relocation required", "relocation required", False),
        # A denial in another clause, or well before the phrase, is about something else.
        ("We do not sponsor visas; relocation required", "relocation required", True),
        ("We do not offer sponsorship and relocation is required", "relocation required", True),
        ("Active security clearance", "security clearance", True),
        ("No security clearance needed", "security clearance", False),
        ("k8s on-call", "kubernetes", True),  # aliases still apply
        ("anything", "", False),
    ],
)
def test_states_is_said_not_denied(text: str, phrase: str, expected: bool) -> None:
    assert states(text, phrase) is expected


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


def test_html_to_text_keeps_cells_and_side_by_side_elements_apart() -> None:
    assert (
        html_to_text("<table><tr><td>Kubernetes</td><td>AWS</td></tr></table>") == "Kubernetes AWS"
    )
    assert html_to_text("<span>Kubernetes</span><span>AWS</span>") == "Kubernetes AWS"
    assert (
        html_to_text("<b>K</b>ubernetes on <i>AWS</i>") == "Kubernetes on AWS"
    )  # one word stays one
    assert html_to_text("<p>a</p><!-- hidden --><script>alert(1)</script><p>b</p>") == "a\n\nb"


def test_html_to_text_cost_grows_in_step_with_the_input() -> None:
    import time

    started = time.perf_counter()
    assert len(html_to_text("<p>hello world</p>" * 10_000)) > 100_000
    html_to_text("<br>" * 8_000)
    html_to_text("<a" * 100_000)  # unterminated tags
    html_to_text("<div>" * 5_000 + "deep" + "</div>" * 5_000)
    assert time.perf_counter() - started < 3.0  # these took a minute and more before
