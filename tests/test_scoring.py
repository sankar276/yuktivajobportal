from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.config import Seniority, UserConfig
from jobportal.crawl import add_source, crawl
from jobportal.http import PoliteClient
from jobportal.models import Decision, Job, JobScore, User
from jobportal.scoring import (
    infer_seniority,
    is_blocked,
    judge_location,
    score_job,
    score_jobs,
    search_terms,
    title_matches_any_lane,
)
from jobportal.sources import SourceSpec
from tests.conftest import NOW, FakeWeb, fixture_json

DESCRIPTION = (
    "Own the architecture of our Kubernetes platform on AWS, delivered with Terraform "
    "and GitOps (ArgoCD). Zero trust identity with Vault. Operators in Go. Kafka."
)


@dataclass
class FakeJob:
    title: str = "Principal Platform Engineer"
    company_name: str = "Acme Robotics"
    location: str = "Remote - US"
    remote: bool | None = True
    employment_type: str | None = "full_time"
    description_text: str = DESCRIPTION
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    comp_period: str | None = None
    needs_detail: bool = False
    posted: datetime | None = NOW - timedelta(hours=3)
    facts: dict = field(default_factory=dict)

    @property
    def effective_posted_at(self) -> datetime | None:
        return self.posted


# ---------------------------------------------------------------- seniority


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Software Engineer", Seniority.mid),
        ("Software Engineer II", Seniority.mid),
        ("Software Engineer III", Seniority.senior),
        ("Junior DevOps Engineer", Seniority.junior),
        ("Associate Cloud Engineer", Seniority.junior),
        ("Senior Platform Engineer", Seniority.senior),
        ("Sr. SRE", Seniority.senior),
        ("Staff Platform Engineer", Seniority.staff),
        ("Lead DevOps Engineer", Seniority.staff),
        ("Senior Staff Engineer", Seniority.principal),
        ("Principal Engineer", Seniority.principal),
        ("Distinguished Engineer", Seniority.principal),
        ("Cloud Architect", Seniority.staff),
        ("Senior Solutions Architect", Seniority.principal),
        ("Engineering Manager", Seniority.staff),
        ("Senior Manager, Platform", Seniority.principal),
        ("Director of Platform Engineering", Seniority.director),
        ("Senior Director, Infrastructure", Seniority.director),
        ("Head of Platform", Seniority.director),
        ("Chief Architect", Seniority.director),
        ("VP, Engineering", Seniority.vp),
        ("Vice President of Infrastructure", Seniority.vp),
        ("Chief Technology Officer", Seniority.executive),
        ("SVP Engineering", Seniority.executive),
        ("CTO", Seniority.executive),
    ],
)
def test_infer_seniority(title: str, level: Seniority) -> None:
    assert infer_seniority(title) is level


# ----------------------------------------------------------------- location


@pytest.mark.parametrize(
    ("location", "remote", "value", "fails"),
    [
        ("Remote - US", True, 1.0, False),
        ("Remote (United States)", True, 1.0, False),
        ("US, TX, Remote", True, 1.0, False),
        ("Remote - Texas", True, 1.0, False),
        ("Remote", True, 0.9, False),
        ("", True, 0.9, False),
        ("San Francisco", True, 0.9, False),  # remote flag, city only: region unknown
        ("Remote - European Union", True, 0.0, True),
        ("Remote - Canada", True, 0.0, True),
        ("CA, ON, Toronto; Remote", True, 0.9, False),  # second segment is unrestricted
        ("CA, ON, Toronto", True, 0.0, True),  # Canada, not California
        ("London, UK; Remote - US", True, 1.0, False),  # any acceptable place is enough
        ("Austin, TX", False, 1.0, False),
        ("Austin, TX", None, 1.0, False),
        ("New York, NY", False, 0.0, True),
        ("New York, NY", None, 0.0, True),
        ("", None, 0.5, False),
        ("United States", None, 0.7, False),  # country-wide: probably remote
        ("United States", False, 0.0, True),  # explicitly not remote
        ("Dublin", None, 0.0, True),
    ],
)
def test_judge_location(
    user_config: UserConfig, location: str, remote: bool | None, value: float, fails: bool
) -> None:
    lane = user_config.search.lanes[0]  # remote in the US, or on-site in Austin / TX
    got_value, note, got_fails = judge_location(FakeJob(location=location, remote=remote), lane)
    assert (got_value, got_fails) == (value, fails), note


def test_lane_without_remote_only_accepts_listed_places(user_config: UserConfig) -> None:
    lane = user_config.search.lanes[0].model_copy(deep=True)
    lane.locations.remote = False
    assert judge_location(FakeJob(location="Remote - US"), lane)[2] is True
    assert judge_location(FakeJob(location="Remote; Austin, TX"), lane)[0] == 1.0


# ------------------------------------------------------------------ scoring


def test_strong_match_is_shortlisted_with_reasons(user_config: UserConfig) -> None:
    result = score_job(FakeJob(), user_config.search, now=NOW)
    assert result.lane == "career"
    assert result.decision is Decision.shortlist
    assert result.score >= 90
    assert any("principal platform engineer" in reason for reason in result.reasons)
    assert any("core skills" in reason for reason in result.reasons)
    assert result.breakdown["core_found"][:3] == ["Kubernetes", "AWS", "Terraform"]
    assert sum(f["points"] for f in result.breakdown["factors"]) == pytest.approx(
        result.score, abs=0.3
    )


def test_scores_are_bounded_and_deterministic(user_config: UserConfig) -> None:
    job = FakeJob()
    first = score_job(job, user_config.search, now=NOW)
    assert first == score_job(job, user_config.search, now=NOW)
    assert 0 <= first.score <= 100


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"title": "Account Executive"}, "matches none of this lane's titles"),
        ({"title": "Platform Architect Intern"}, "excluded 'intern'"),
        ({"location": "Remote - European Union"}, "outside your regions"),
        ({"location": "New York, NY", "remote": False}, "New York, NY"),
        ({"description_text": DESCRIPTION + " Relocation required."}, "relocation required"),
        ({"facts": {"travel_percent": 50}}, "50% travel"),
        (
            {
                "comp_min": 120000.0,
                "comp_max": 150000.0,
                "comp_currency": "USD",
                "comp_period": "year",
            },
            "below your floor",
        ),
        ({"employment_type": "part_time"}, "not what this lane is for"),
    ],
)
def test_hard_filters_skip_with_the_rule_named(
    user_config: UserConfig, change: dict, reason: str
) -> None:
    result = score_job(FakeJob(**change), user_config.search, now=NOW)
    assert result.decision is Decision.skip
    assert result.score == 0
    assert any(reason in r for r in result.reasons), result.reasons


def test_blocked_company_is_skipped_whatever_the_fit(user_config: UserConfig) -> None:
    result = score_job(FakeJob(company_name="Northwind Systems, Inc."), user_config.search, now=NOW)
    assert result.decision is Decision.skip
    assert "blocked list" in result.reasons[0]


def test_is_blocked_matches_whole_names_only() -> None:
    blocked = ["Northwind Systems", "OCC"]
    assert is_blocked("Northwind Systems LLC", blocked) == "Northwind Systems"
    assert is_blocked("Northwind Systems Europe", blocked) == "Northwind Systems"
    assert is_blocked("OCC", blocked) == "OCC"
    assert is_blocked("Occidental Petroleum", blocked) is None
    assert is_blocked("Northwind Traders", blocked) is None


def test_contract_role_lands_in_the_contract_lane(user_config: UserConfig) -> None:
    job = FakeJob(
        title="Cloud Engineer (Contract)",
        employment_type="contract",
        location="Dallas, TX",
        remote=False,
        description_text="AWS landing zones with Terraform and Python. Kubernetes a plus. CI/CD.",
        comp_min=95,
        comp_max=110,
        comp_currency="USD",
        comp_period="hour",
    )
    result = score_job(job, user_config.search, now=NOW)
    assert result.lane == "contract"
    assert result.decision is Decision.shortlist
    assert result.breakdown["lanes"]["career"]["skip"]
    assert any("USD 95-110 per hour" in reason for reason in result.reasons)


def test_low_rate_contract_is_skipped(user_config: UserConfig) -> None:
    job = FakeJob(
        title="Cloud Engineer",
        employment_type="contract",
        comp_min=50,
        comp_max=60,
        comp_currency="USD",
        comp_period="hour",
    )
    result = score_job(job, user_config.search, now=NOW)
    assert result.decision is Decision.skip
    assert any("below your floor of 85" in reason for reason in result.reasons)


def test_one_level_below_the_lane_is_penalised_two_is_skipped(user_config: UserConfig) -> None:
    senior = score_job(FakeJob(title="Senior Platform Architect"), user_config.search, now=NOW)
    assert senior.decision is not Decision.skip  # senior + architect reads as principal
    below = score_job(FakeJob(title="Senior Cloud Architect II"), user_config.search, now=NOW)
    assert below.decision is not Decision.skip
    junior = score_job(FakeJob(title="Junior Cloud Architect"), user_config.search, now=NOW)
    assert junior.decision is Decision.skip
    assert any("this lane starts at staff" in r for r in junior.reasons)


def test_freshness_decays(user_config: UserConfig) -> None:
    scores = [
        score_job(FakeJob(posted=NOW - age), user_config.search, now=NOW).score
        for age in (timedelta(hours=2), timedelta(days=2), timedelta(days=10), timedelta(days=45))
    ]
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == 4
    unknown = score_job(FakeJob(posted=None), user_config.search, now=NOW)
    assert any("Age unknown" in r for r in unknown.reasons)


def test_weak_skills_match_is_considered_not_shortlisted(user_config: UserConfig) -> None:
    job = FakeJob(
        title="Staff Platform Engineer",
        description_text="We use Java and Oracle.",
        posted=NOW - timedelta(days=20),
    )
    result = score_job(job, user_config.search, now=NOW)
    assert result.decision is Decision.consider
    assert result.score < 60


def test_stub_without_description_is_never_shortlisted(user_config: UserConfig) -> None:
    result = score_job(FakeJob(description_text="", needs_detail=True), user_config.search, now=NOW)
    assert result.decision is Decision.consider
    assert result.breakdown["provisional"] is True


def test_title_filter_and_search_terms(user_config: UserConfig) -> None:
    search = user_config.search
    assert title_matches_any_lane(search, "Sr. Platform Architect")
    assert title_matches_any_lane(search, "DevOps Engineer")  # contract lane, related
    assert not title_matches_any_lane(search, "Marketing Coordinator")
    assert not title_matches_any_lane(search, "Platform Engineer Intern")
    terms = search_terms(search)
    assert "platform architect" in terms and len(terms) == len(set(terms)) <= 10


# -------------------------------------------------------------- persistence


def test_score_jobs_persists_and_skips_unchanged(
    session: Session, user: User, user_config: UserConfig, client: PoliteClient, web: FakeWeb
) -> None:
    add_source(session, SourceSpec(kind="greenhouse", token="acme"))
    session.commit()
    web.json(
        "GET",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
        fixture_json("greenhouse_jobs.json"),
    )
    crawl(session, client, now=NOW)

    stats = score_jobs(session, user.id, user_config.search, now=NOW)
    session.commit()
    assert (stats.scored, stats.shortlisted, stats.skipped) == (3, 1, 2)

    scores = {
        job.title: score
        for job, score in session.execute(
            select(Job, JobScore).join(JobScore, JobScore.job_id == Job.id)
        )
    }
    best = scores["Principal Platform Engineer"]
    assert best.decision == "shortlist" and best.lane == "career" and best.score > 80
    assert scores["Account Executive, EMEA"].decision == "skip"
    # The SRE role is senior-level and on-site in Austin: below the career lane, wrong type for contract.
    assert scores["Senior Site Reliability Engineer"].decision == "skip"

    again = score_jobs(session, user.id, user_config.search, now=NOW + timedelta(hours=1))
    assert (again.scored, again.unchanged) == (0, 3)
    stale = score_jobs(session, user.id, user_config.search, now=NOW + timedelta(hours=13))
    assert stale.scored == 3  # freshness moves, so scores expire

    changed = user_config.search.model_copy(deep=True)
    changed.lanes[0].shortlist_at = 99
    rescored = score_jobs(session, user.id, changed, now=NOW + timedelta(hours=13))
    assert rescored.scored == 3 and rescored.shortlisted == 0


# -------------------------------------------------------------------- facts


def test_clearance_and_sponsorship_requirements_skip_when_you_cannot_meet_them(
    user_config: UserConfig,
) -> None:
    search = user_config.search
    profile = user_config.profile.model_copy(deep=True)
    needs_clearance = FakeJob(facts={"clearance": "required", "clearance_level": "TS/SCI"})

    result = score_job(needs_clearance, search, now=NOW, profile=profile)
    assert result.decision is Decision.skip
    assert result.reasons == ["Requires an active TS/SCI security clearance"]

    profile.security_clearance = "TS/SCI"
    assert (
        score_job(needs_clearance, search, now=NOW, profile=profile).decision is Decision.shortlist
    )
    # "able to obtain" is not a requirement to hold one today
    obtainable = FakeJob(facts={"clearance": "obtainable"})
    assert (
        score_job(obtainable, search, now=NOW, profile=user_config.profile).decision
        is Decision.shortlist
    )

    no_sponsor = FakeJob(facts={"sponsorship": "not_offered"})
    assert (
        score_job(no_sponsor, search, now=NOW, profile=user_config.profile).decision
        is Decision.shortlist
    )
    profile.work_authorization.needs_sponsorship = True
    blocked = score_job(no_sponsor, search, now=NOW, profile=profile)
    assert blocked.decision is Decision.skip
    assert "sponsorship is not available" in blocked.reasons[0]


def test_years_asked_beyond_your_profile_is_noted_not_skipped(user_config: UserConfig) -> None:
    search, profile = user_config.search, user_config.profile
    result = score_job(FakeJob(facts={"years_required": 20}), search, now=NOW, profile=profile)
    assert result.decision is Decision.shortlist
    assert "Asks for 20+ years; your profile says 15" in result.reasons
    fine = score_job(FakeJob(facts={"years_required": 10}), search, now=NOW, profile=profile)
    assert not any("Asks for" in reason for reason in fine.reasons)


def test_travel_within_the_lane_limit_is_fine(user_config: UserConfig) -> None:
    job = FakeJob(facts={"travel_percent": 25})
    assert score_job(job, user_config.search, now=NOW).decision is Decision.shortlist
