from __future__ import annotations

import pytest

from jobportal.facts import extract_facts, workplace_of


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("10+ years of experience in infrastructure engineering", 10),
        ("12+ years of software experience, including 5+ years of experience leading teams", 12),
        ("Minimum of 8 years in platform roles", 8),
        ("At least 7 years building distributed systems", 7),
        ("8-10 years of relevant experience", 8),
        ("15 yrs experience with cloud", 15),
        ("5 or more years of professional experience", 5),
        ("We have been in business for 20 years.", None),
        # Asked for without the word "experience": the plus says it is a requirement.
        ("8+ years in software engineering", 8),
        ("You have 15+ years in the industry", 15),
        ("Experience: 10+ years", 10),
        ("5+ yrs of exp in cloud", 5),
        ("7+ YOE", 7),
        ("at least five years of experience", 5),
        ("Minimum ten years of experience", 10),
        ("min. 3 years", 3),
        ("5-7+ years of experience", 5),
        # The company's years, not yours.
        ("We are a 10 year old company with 15 years of combined founder experience", None),
        ("Over the last 25 years of experience serving customers, we grew.", None),
        ("Our leadership team brings 30 years of experience", None),
        ("With 20 years of experience in the market, Acme is a leader", None),
        ("vesting over at least 4 years", None),
        ("You don't need 10 years of experience to apply", None),
        # A nice-to-have is not the bar.
        ("Nice to have: 20+ years of experience with COBOL. Required: 5+ years of Python", 5),
        ("2+ years of experience with Rust preferred. 10+ years of experience overall", 10),
        ("10+ years of experience, cloud certification preferred", 10),
        ("- Kubernetes is a plus\n- 8+ years of experience", 8),
        ("401(k) with 4% match after 2 years", None),
        ("Experience with Kubernetes", None),
        ("", None),
    ],
)
def test_years_required(text: str, years: int | None) -> None:
    assert extract_facts(text).get("years_required") == years


@pytest.mark.parametrize(
    ("text", "clearance", "level"),
    [
        ("Active TS/SCI clearance required.", "required", "TS/SCI"),
        ("Must have a current Secret clearance.", "required", "Secret"),
        ("This position requires a Top Secret security clearance.", "required", "Top Secret"),
        ("Security clearance is required for this role.", "required", None),
        ("Must be able to obtain a security clearance.", "obtainable", None),
        ("Ability to obtain and maintain a Public Trust clearance.", "obtainable", "Public Trust"),
        ("We keep customer secrets safe with Vault.", None, None),
        ("Clear communication skills.", None, None),
    ],
)
def test_clearance(text: str, clearance: str | None, level: str | None) -> None:
    facts = extract_facts(text)
    assert facts.get("clearance") == clearance
    assert facts.get("clearance_level") == level


@pytest.mark.parametrize(
    ("text", "sponsorship"),
    [
        ("We are unable to sponsor visas for this position.", "not_offered"),
        ("This role is not eligible for visa sponsorship.", "not_offered"),
        ("Must be authorized to work in the US without sponsorship.", "not_offered"),
        ("Visa sponsorship is not available.", "not_offered"),
        ("Visa sponsorship is available for exceptional candidates.", "offered"),
        ("We will sponsor work visas.", "offered"),
        ("Our sponsors include several foundations.", None),
        ("Great benefits.", None),
    ],
)
def test_sponsorship(text: str, sponsorship: str | None) -> None:
    assert extract_facts(text).get("sponsorship") == sponsorship


@pytest.mark.parametrize(
    ("text", "travel"),
    [
        ("Up to 25% travel.", 25),
        ("Travel up to 50% of the time.", 50),
        ("Requires 10% domestic travel", 10),
        ("Travel: 20-30%", 30),
        ("We match 4% of salary. Travel is rare.", None),
        ("No travel required.", None),
    ],
)
def test_travel(text: str, travel: int | None) -> None:
    assert extract_facts(text).get("travel_percent") == travel


@pytest.mark.parametrize(
    ("text", "education"),
    [
        ("Bachelor's degree in Computer Science; Master's preferred.", "bachelor"),
        ("BS/BA in a technical field", "bachelor"),
        ("Master's degree or PhD in machine learning", "master"),
        ("Ph.D. in physics", "phd"),
        ("Manage the master branch and master data.", None),
        ("No degree required.", None),
    ],
)
def test_education(text: str, education: str | None) -> None:
    assert extract_facts(text).get("education") == education


def test_oncall_and_certifications() -> None:
    facts = extract_facts(
        "Participate in the on-call rotation. CKA or CKS preferred; CISSP a plus."
    )
    assert facts["oncall"] is True
    assert facts["certifications"] == ["CKA", "CKS", "CISSP"]
    assert "oncall" not in extract_facts("Call us on Monday.")


@pytest.mark.parametrize(
    ("remote", "location", "description", "declared", "expected"),
    [
        (True, "Remote - US", "", None, "remote"),
        (None, "Austin, TX", "", "hybrid", "hybrid"),
        (None, "Austin, TX (Hybrid)", "", None, "hybrid"),
        (True, "Remote", "", "on-site", "onsite"),  # the ATS's explicit field wins
        (None, "Austin, TX", "This is a hybrid role, three days in the office.", None, "hybrid"),
        (False, "Austin, TX", "", None, "onsite"),
        (None, "Austin, TX", "On-site 5 days a week is required.", None, "onsite"),
        (None, "Austin, TX", "Great team.", None, None),
    ],
)
def test_workplace(remote, location, description, declared, expected) -> None:
    assert workplace_of(remote, location, description, declared) == expected


def test_only_stated_facts_are_reported() -> None:
    assert extract_facts("Build platforms with Kubernetes.") == {}


# ----------------------------------------------- precision (review findings)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("No security clearance required.", None),
        ("This role does not require a security clearance.", None),
        ("An active security clearance is a plus but not required.", None),
        ("Employment requires successful clearance of a background check.", None),
        ("Candidates with an active clearance are encouraged to apply.", None),
        ("Active clearance preferred.", None),
        (
            "Must be able to obtain and maintain a Top Secret clearance. Active clearance preferred.",
            "obtainable",
        ),
        ("Ability to obtain a security clearance.", "obtainable"),
        ("Interim Secret clearance or higher is required to start", "required"),
        ("Active Secret clearance required", "required"),
        ("Must hold an active TS/SCI clearance.", "required"),
        ("- Active TS/SCI clearance", "required"),
        ("This position requires a Top Secret clearance.", "required"),
        ("Security clearance required: None", None),
        # Somebody else's requirement, or a process, is not asked of you.
        (
            "You will work with customers who require FedRAMP and security clearance processes.",
            None,
        ),
    ],
)
def test_clearance_is_required_only_when_the_posting_says_so(
    text: str, expected: str | None
) -> None:
    assert extract_facts(text).get("clearance") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We cover 100% of your travel costs for offsites.", None),
        ("Benefits include travel reimbursement, 100% employer-paid health insurance.", None),
        ("Remote: 100%\nTravel: 10%", 10),
        ("This role is 100% remote with occasional travel", None),
        ("Travel is minimal and our 401(k) match is 50% of contributions", None),
        ("Improved uptime to 99.99% for the travel booking API", None),
        ("Up to 25% travel", 25),
        ("Travel up to 20% of the time", 20),
        ("Willingness to travel 30%", 30),
        ("up to 15% international travel", 15),
        ("Travel required: approximately 40%", 40),
        ("Occasional travel (approximately 10 percent)", 10),
        ("Travel benefits: 50% off flights", None),
        ("Reduce travel time by 40% for our customers", None),
        ("We reduced business travel emissions by 35%", None),
        ("Some travel 100% remote", None),
    ],
)
def test_travel_is_only_a_number_that_belongs_to_travel(text: str, expected: int | None) -> None:
    assert extract_facts(text).get("travel_percent") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Visa sponsorship is not currently available for this role.", "not_offered"),
        ("Sponsorship cannot be offered", "not_offered"),
        ("We are unable to sponsor visas.", "not_offered"),
        ("Must be authorized to work in the US without sponsorship.", "not_offered"),
        ("This is not an entry-level role and sponsorship is available", "offered"),
        ("Visa sponsorship is available.", "offered"),
        ("We will sponsor H-1B visas.", "offered"),
        ("Open to candidates with or without sponsorship", None),
        ("Employment sponsorship isn't offered.", "not_offered"),
        ("Sponsorship unavailable.", "not_offered"),
        ("We are unable to sponsor candidates at this time.", "not_offered"),
        ("Visa sponsorship: No", "not_offered"),
        ("Sponsorship: Not available", "not_offered"),
        ("Visa sponsorship: Yes", "offered"),
        ("We are happy to sponsor visas; no relocation is offered", "offered"),
        # Sponsoring something other than a person's right to work.
        ("You will not manage event sponsorship budgets", None),
        ("No prior experience with sponsorship programs is needed", None),
        ("We don't just sponsor conferences, we speak at them", None),
        ("Experience with sponsor banks is not required", None),
        ("We sponsor the Kubernetes community", None),
        ("We will sponsor your conference attendance", None),
    ],
)
def test_sponsorship_is_read_clause_by_clause(text: str, expected: str | None) -> None:
    assert extract_facts(text).get("sponsorship") == expected


def test_a_hybrid_cloud_is_not_a_hybrid_workplace() -> None:
    assert "workplace" not in extract_facts("You will run our hybrid cloud across AWS and on-prem.")
    hybrid = extract_facts("This is a hybrid role with three days a week in the office.")
    assert hybrid["workplace"] == "hybrid"


def test_hostile_whitespace_cannot_stall_extraction() -> None:
    import time

    started = time.perf_counter()
    extract_facts("5" + "\f" * 20_000)  # took hours before; see the security review
    extract_facts("5 " * 60_000)
    extract_facts("10+ years\tof experience")
    assert time.perf_counter() - started < 2.0
    assert extract_facts("10+ years of\nprofessional experience")["years_required"] == 10
