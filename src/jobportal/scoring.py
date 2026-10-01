"""Score a posting against your lanes.

Deterministic and explainable: every score comes with the reasons behind it,
and every skip names the rule that caused it. A lane first applies hard
filters (blocked company, wrong employment type, excluded title, location you
would not work in, pay below your floor) and then a weighted rubric
(title, skills, seniority, location, freshness) on a 0-100 scale.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.config import Employment, Lane, SearchConfig, Seniority
from jobportal.db import utcnow
from jobportal.models import Decision, Job, JobScore
from jobportal.text import company_key, find_terms, phrase_in_title, sha256_text, title_words

RESCORE_AFTER = timedelta(hours=12)


class Scorable(Protocol):
    title: str
    company_name: str
    location: str
    remote: bool | None
    employment_type: str | None
    description_text: str
    comp_min: float | None
    comp_max: float | None
    comp_currency: str | None
    comp_period: str | None
    needs_detail: bool

    @property
    def effective_posted_at(self) -> datetime | None: ...


@dataclass
class Factor:
    name: str
    value: float  # 0..1
    weight: float
    note: str

    @property
    def points(self) -> float:
        return self.value * self.weight


@dataclass
class LaneResult:
    lane: str
    score: float = 0.0
    skip: str = ""  # the rule that ruled the job out; empty when it was scored
    provisional: bool = False
    factors: list[Factor] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    core_found: list[str] = field(default_factory=list)
    bonus_found: list[str] = field(default_factory=list)


@dataclass
class ScoreResult:
    lane: str | None
    score: float
    decision: Decision
    reasons: list[str]
    breakdown: dict[str, Any]


# ---------------------------------------------------------------- seniority

_JUNIOR = {
    "junior",
    "associate",
    "entry",
    "intern",
    "internship",
    "graduate",
    "trainee",
    "apprentice",
    "avp",
}
_EXECUTIVE = {"cto", "cio", "ciso", "ceo", "coo", "cfo", "cpo", "svp", "evp", "president"}
_CHIEF_IC = {"architect", "engineer", "scientist", "staff", "technologist"}
_LEVEL_NUMBERS = {"1": Seniority.junior, "2": Seniority.mid, "3": Seniority.senior,
                  "4": Seniority.staff, "5": Seniority.principal}  # fmt: skip


def infer_seniority(title: str) -> Seniority:
    """Read a level off a job title. Titles vary by company, so this is a best guess."""
    words = title_words(title)
    present = set(words)
    senior = "senior" in present

    if present & _EXECUTIVE:
        return Seniority.executive
    if "chief" in present:
        # "Chief Architect" is a top individual contributor, not the C-suite.
        return Seniority.director if present & _CHIEF_IC else Seniority.executive
    if "vp" in present:
        return Seniority.vp
    if "director" in present or "head" in present:
        return Seniority.director
    if present & {"principal", "distinguished", "fellow"}:
        return Seniority.principal
    if "manager" in present:
        return Seniority.principal if senior else Seniority.staff
    if "staff" in present or "lead" in present:
        return Seniority.principal if senior else Seniority.staff
    if "architect" in present:
        if present & _JUNIOR:
            return Seniority.mid
        return Seniority.principal if senior else Seniority.staff
    if senior:
        return Seniority.senior
    if present & _JUNIOR:
        return Seniority.junior
    if words and words[-1] in _LEVEL_NUMBERS:
        return _LEVEL_NUMBERS[words[-1]]
    return Seniority.mid


# ----------------------------------------------------------------- location

_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}  # fmt: skip
_US_NAMES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america", "america"}
# Two-letter codes are only trusted in upper case ("IN" the state vs "in" the word).
_US_CODE_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(sorted(set(_US_STATES.values()) | {"US", "USA"}))
    + r")(?![A-Za-z])"
)
_US_NAME_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(n) for n in sorted(_US_STATES, key=len, reverse=True))
    + r"|united states(?: of america)?|u\.s\.a?\.?)(?![A-Za-z])",
    re.IGNORECASE,
)
_ELSEWHERE = [
    "Canada", "Mexico", "Brazil", "Argentina", "Colombia", "Chile", "Costa Rica", "LATAM",
    "Latin America", "UK", "United Kingdom", "England", "Scotland", "Ireland", "Germany",
    "France", "Spain", "Portugal", "Italy", "Netherlands", "Belgium", "Poland", "Romania",
    "Sweden", "Norway", "Denmark", "Finland", "Switzerland", "Austria", "Czech", "Hungary",
    "Bulgaria", "Serbia", "Croatia", "Greece", "Estonia", "Lithuania", "Latvia", "Ukraine",
    "Turkey", "Israel", "UAE", "Saudi Arabia", "India", "Pakistan", "Sri Lanka", "Bangladesh",
    "Singapore", "Japan", "China", "Hong Kong", "Taiwan", "Korea", "Philippines", "Vietnam",
    "Indonesia", "Malaysia", "Thailand", "Australia", "New Zealand", "South Africa", "Nigeria",
    "Kenya", "Egypt", "Europe", "European Union", "EU", "EMEA", "APAC", "APJ", "Asia",
    "Africa", "Middle East", "London", "Dublin", "Berlin", "Munich", "Paris", "Amsterdam",
    "Madrid", "Barcelona", "Lisbon", "Warsaw", "Stockholm", "Zurich", "Toronto", "Vancouver",
    "Montreal", "Ontario", "Quebec", "British Columbia", "Alberta", "Bangalore", "Bengaluru",
    "Hyderabad", "Pune", "Chennai", "Mumbai", "Gurgaon", "Noida", "Sydney", "Melbourne",
    "Tokyo", "Tel Aviv", "Sao Paulo", "São Paulo",
]  # fmt: skip
_ELSEWHERE_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(re.escape(place) for place in sorted(_ELSEWHERE, key=len, reverse=True))
    # Workday writes Canada as "CA, ON, Toronto": country code, then province.
    + r"|CA,\s*(?:ON|BC|QC|AB|MB|SK|NS|NB)"
    + r")(?![A-Za-z])"
)
_SEGMENT_RE = re.compile(r"\s*(?:;|\||\n|\s+or\s+|\s+/\s+)\s*")


def mentions_us(text: str) -> bool:
    return bool(_US_CODE_RE.search(text) or _US_NAME_RE.search(text))


def _place_in(place: str, text: str) -> bool:
    """Whole-word match; short all-caps places ("TX", "US") are case-sensitive."""
    flags = 0 if (len(place) <= 3 and place.isupper()) else re.IGNORECASE
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(place)}(?![A-Za-z])", text, flags))


def _wants_us(regions: list[str]) -> bool:
    return any(region.strip().lower() in _US_NAMES for region in regions)


def _segments(text: str) -> list[str]:
    """A posting often lists several places; each is judged on its own."""
    return [part for part in _SEGMENT_RE.split(text) if part.strip()] or ([text] if text else [])


def _region_fit(segment: str, regions: list[str]) -> str:
    """``yes`` / ``no`` / ``unknown``: is this place inside the regions you accept?"""
    elsewhere = _ELSEWHERE_RE.search(segment)
    inside = any(_place_in(region, segment) for region in regions) or (
        _wants_us(regions) and mentions_us(segment)
    )
    if inside and not elsewhere:
        return "yes"
    return "no" if elsewhere else "unknown"


def _is_countrywide(segment: str, regions: list[str]) -> bool:
    """The whole location is just a country or region you accept ("United States")."""
    bare = re.sub(r"[^a-z. ]+", " ", segment.lower()).strip()
    names = {region.strip().lower() for region in regions}
    if _wants_us(regions):
        names |= _US_NAMES
    return bare in names


def judge_location(job: Scorable, lane: Lane) -> tuple[float, str, bool]:
    """``(value, note, hard_fail)`` for how the job's location fits the lane."""
    rules = lane.locations
    text = job.location or ""
    shown = f" ({text})" if text else ""
    onsite_hit = next((place for place in rules.onsite if _place_in(place, text)), None)

    if job.remote:
        if not rules.remote:
            if onsite_hit:
                return 1.0, f"Remote role, and {onsite_hit} is on your list", False
            return 0.0, "Remote role; this lane is on-site only", True
        if not rules.remote_regions:
            return 1.0, f"Remote{shown}", False
        fits = [_region_fit(segment, rules.remote_regions) for segment in _segments(text)]
        if "yes" in fits:
            return 1.0, f"Remote{shown}", False
        if onsite_hit:
            return 1.0, f"Remote or {onsite_hit}{shown}", False
        if not fits or "unknown" in fits:
            return 0.9, f"Remote; region not stated{shown}", False
        return 0.0, f"Remote, but outside your regions{shown}", True

    if onsite_hit:
        kind = "On-site or hybrid" if job.remote is False else "Based"
        return 1.0, f"{kind} in {onsite_hit}{shown}", False
    if not text:
        return 0.5, "Location not stated", False
    if (
        job.remote is None
        and rules.remote
        and any(_is_countrywide(segment, rules.remote_regions) for segment in _segments(text))
    ):
        # "United States" with no city usually means remote within the country.
        return 0.7, f"Country-wide location, likely remote{shown}", False
    if not rules.onsite and not rules.remote:
        return 0.5, f"No location rules set for this lane{shown}", False
    wanted = "remote" if rules.remote and not rules.onsite else "in your locations"
    return 0.0, f"Not {wanted}: {text}", True


# ------------------------------------------------------------------ factors


def _title_factor(job: Scorable, lane: Lane) -> tuple[float, str, bool]:
    words = title_words(job.title)
    rules = lane.titles
    excluded = next((p for p in rules.exclude if phrase_in_title(p, words)), None)
    if excluded:
        return 0.0, f"Title contains excluded '{excluded}'", True
    target = next((p for p in rules.target if phrase_in_title(p, words)), None)
    if target:
        return 1.0, f"Title matches target '{target}'", False
    related = next((p for p in rules.related if phrase_in_title(p, words)), None)
    if related:
        return 0.65, f"Title matches related '{related}'", False
    if rules.required and (rules.target or rules.related):
        return 0.0, "Title matches none of this lane's titles", True
    return 0.0, "Title is not one of this lane's titles", False


def _seniority_factor(job: Scorable, lane: Lane) -> tuple[float, str, bool]:
    level = infer_seniority(job.title)
    low, high = lane.seniority.min, lane.seniority.max
    if low is not None and level < low:
        gap = low - level
        note = f"Reads as {level.name} level; this lane starts at {low.name}"
        return (0.4, note, False) if gap == 1 else (0.0, note, True)
    if high is not None and level > high:
        gap = level - high
        note = f"Reads as {level.name} level; this lane tops out at {high.name}"
        return (0.5, note, False) if gap == 1 else (0.0, note, True)
    return 1.0, f"{level.name.capitalize()} level", False


def _skills_factor(job: Scorable, lane: Lane, result: LaneResult) -> tuple[float, str, bool]:
    rules = lane.skills
    text = f"{job.title}\n{job.description_text or ''}"
    if job.needs_detail:
        result.provisional = True
        return 0.5, "Description not fetched yet", False
    result.core_found = find_terms(text, rules.core)
    result.bonus_found = find_terms(text, rules.bonus)
    if rules.must_have_any and not find_terms(text, rules.must_have_any):
        return 0.0, "None of your must-have skills appear in the posting", True
    if not rules.core:
        return 0.5, "No core skills set for this lane", False
    full_marks = min(rules.full_marks_at, len(rules.core))
    core_part = min(1.0, len(result.core_found) / full_marks)
    if rules.bonus:
        bonus_part = min(1.0, len(result.bonus_found) / min(3, len(rules.bonus)))
        value = 0.8 * core_part + 0.2 * bonus_part
    else:
        value = core_part
    if result.core_found:
        shown = ", ".join(result.core_found[:8])
        note = f"{len(result.core_found)} of your core skills in the posting: {shown}"
    else:
        note = "None of your core skills appear in the posting"
    return value, note, False


def _freshness_factor(job: Scorable, now: datetime, fresh_hours: int) -> tuple[float, str]:
    posted = job.effective_posted_at
    if posted is None:
        return 0.3, "Age unknown (already listed when this source was first read)"
    hours = max(0.0, (now - posted).total_seconds() / 3600)
    if hours <= fresh_hours:
        return 1.0, f"Posted {_age(hours)} ago"
    for limit_days, value in ((3, 0.8), (7, 0.6), (14, 0.4), (30, 0.2)):
        if hours <= limit_days * 24:
            return value, f"Posted {_age(hours)} ago"
    return 0.05, f"Posted {_age(hours)} ago"


def _age(hours: float) -> str:
    if hours < 1:
        return "under an hour"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)} days"


def _pay_check(job: Scorable, lane: Lane) -> tuple[str, bool]:
    rules = lane.compensation
    if job.comp_max is None or not job.comp_period:
        return "", False
    currency = job.comp_currency or rules.currency
    low = f"{job.comp_min:,.0f}" if job.comp_min is not None else "?"
    shown = f"{currency} {low}-{job.comp_max:,.0f} per {job.comp_period}"
    if currency != rules.currency:
        return f"Advertised pay: {shown}", False
    floor = rules.min_base if job.comp_period == "year" else rules.min_hourly
    if floor is not None and job.comp_max < floor:
        return f"Advertised pay ({shown}) tops out below your floor of {floor:,.0f}", True
    return f"Advertised pay: {shown}", False


# -------------------------------------------------------------------- lanes


def score_lane(job: Scorable, lane: Lane, *, now: datetime, fresh_hours: int) -> LaneResult:
    result = LaneResult(lane=lane.key)

    if lane.employment:
        allowed = {e.value for e in lane.employment}
        # Company career pages rarely state the obvious; an unlabelled posting is
        # taken to be a regular full-time role.
        kind = job.employment_type or Employment.full_time.value
        if kind not in allowed:
            if job.employment_type:
                label = job.employment_type.replace("_", " ").capitalize()
                result.skip = f"{label} role; not what this lane is for"
            else:
                wanted = " or ".join(sorted(a.replace("_", " ") for a in allowed))
                result.skip = f"Not marked as a {wanted} role"
            return result

    weights = lane.weights
    checks = (
        ("title", weights.title, _title_factor(job, lane)),
        ("seniority", weights.seniority, _seniority_factor(job, lane)),
        ("location", weights.location, judge_location(job, lane)),
        ("skills", weights.skills, _skills_factor(job, lane, result)),
    )
    for name, weight, (value, note, hard_fail) in checks:
        if hard_fail:
            result.skip = note
            return result
        result.factors.append(Factor(name, value, weight, note))

    if not job.needs_detail:
        phrase = next(
            iter(find_terms(job.description_text or "", lane.skip_if_description_has)), None
        )
        if phrase:
            result.skip = f"Posting mentions '{phrase}'"
            return result

    pay_note, pay_fail = _pay_check(job, lane)
    if pay_fail:
        result.skip = pay_note
        return result
    if pay_note:
        result.notes.append(pay_note)

    value, note = _freshness_factor(job, now, fresh_hours)
    result.factors.append(Factor("freshness", value, weights.freshness, note))

    result.score = round(100 * sum(f.points for f in result.factors) / weights.total(), 1)
    return result


def is_blocked(company_name: str, blocked: list[str]) -> str | None:
    """The blocklist entry that matches this company, if any."""
    key = company_key(company_name)
    for entry in blocked:
        entry_key = company_key(entry)
        if entry_key and (entry_key == key or re.search(rf"\b{re.escape(entry_key)}\b", key)):
            return entry
    return None


def score_job(job: Scorable, search: SearchConfig, *, now: datetime | None = None) -> ScoreResult:
    now = now or utcnow()
    blocked = is_blocked(job.company_name, search.blocked_companies)
    if blocked:
        return ScoreResult(
            lane=None,
            score=0.0,
            decision=Decision.skip,
            reasons=[f"{job.company_name} is on your blocked list"],
            breakdown={"blocked": blocked},
        )

    results = [
        score_lane(job, lane, now=now, fresh_hours=search.policy.fresh_hours)
        for lane in search.lanes
    ]
    scored = [r for r in results if not r.skip]
    lanes_summary = {r.lane: ({"skip": r.skip} if r.skip else {"score": r.score}) for r in results}

    if not scored:
        names = {lane.key: lane.name for lane in search.lanes}
        reasons = [f"{names[r.lane]}: {r.skip}" if len(results) > 1 else r.skip for r in results]
        return ScoreResult(
            lane=None,
            score=0.0,
            decision=Decision.skip,
            reasons=reasons,
            breakdown={"lanes": lanes_summary},
        )

    best = max(scored, key=lambda r: r.score)
    lane = search.lane(best.lane)
    assert lane is not None
    shortlisted = best.score >= lane.shortlist_at and not best.provisional
    reasons = [f.note for f in sorted(best.factors, key=lambda f: f.points, reverse=True)]
    reasons += best.notes
    return ScoreResult(
        lane=best.lane,
        score=best.score,
        decision=Decision.shortlist if shortlisted else Decision.consider,
        reasons=reasons,
        breakdown={
            "factors": [
                {
                    "name": f.name,
                    "value": round(f.value, 3),
                    "weight": f.weight,
                    "points": round(100 * f.points / lane.weights.total(), 1),
                    "note": f.note,
                }
                for f in best.factors
            ],
            "core_found": best.core_found,
            "bonus_found": best.bonus_found,
            "provisional": best.provisional,
            "lanes": lanes_summary,
        },
    )


# ------------------------------------------------------------ crawl helpers


def title_matches_any_lane(search: SearchConfig, title: str) -> bool:
    """Would any lane consider this title? Decides which stubs are worth a detail fetch."""
    words = title_words(title)
    for lane in search.lanes:
        rules = lane.titles
        if any(phrase_in_title(p, words) for p in rules.exclude):
            continue
        if not rules.required or not (rules.target or rules.related):
            return True
        if any(phrase_in_title(p, words) for p in (*rules.target, *rules.related)):
            return True
    return False


def search_terms(search: SearchConfig, limit: int = 10) -> tuple[str, ...]:
    """Target titles across all lanes, used to query sources that support search."""
    terms: dict[str, None] = {}
    for lane in search.lanes:
        for phrase in lane.titles.target:
            terms.setdefault(phrase.strip().lower())
    return tuple(list(terms)[:limit])


# -------------------------------------------------------------- persistence


@dataclass
class ScoreStats:
    scored: int = 0
    shortlisted: int = 0
    considered: int = 0
    skipped: int = 0
    unchanged: int = 0


def rubric_hash(search: SearchConfig) -> str:
    payload = search.model_dump(mode="json", exclude={"policy": {"mode", "auto", "email"}})
    return sha256_text(json.dumps(payload, sort_keys=True))


def _input_hash(job: Job, rubric: str) -> str:
    return sha256_text(
        rubric,
        job.title,
        job.company_name,
        job.location,
        str(job.remote),
        job.employment_type,
        job.description_text,
        str(job.comp_min),
        str(job.comp_max),
        job.comp_period,
        str(job.needs_detail),
    )


def score_jobs(
    session: Session,
    user_id: int,
    search: SearchConfig,
    *,
    now: datetime | None = None,
    job_ids: list[int] | None = None,
    force: bool = False,
) -> ScoreStats:
    """Score open jobs that are new, changed, or whose score has gone stale."""
    now = now or utcnow()
    rubric = rubric_hash(search)
    stats = ScoreStats()
    query = select(Job).where(Job.closed_at.is_(None))
    if job_ids is not None:
        query = query.where(Job.id.in_(job_ids))
    existing = {
        score.job_id: score
        for score in session.scalars(select(JobScore).where(JobScore.user_id == user_id))
    }
    for job in session.scalars(query.order_by(Job.id)).yield_per(500):
        current = existing.get(job.id)
        input_hash = _input_hash(job, rubric)
        if (
            not force
            and current is not None
            and current.input_hash == input_hash
            and now - current.scored_at < RESCORE_AFTER
        ):
            stats.unchanged += 1
            continue
        result = score_job(job, search, now=now)
        if current is None:
            current = JobScore(job_id=job.id, user_id=user_id)
            session.add(current)
        current.lane = result.lane
        current.score = result.score
        current.decision = result.decision.value
        current.reasons = result.reasons
        current.breakdown = result.breakdown
        current.input_hash = input_hash
        current.scored_at = now
        stats.scored += 1
        if result.decision is Decision.shortlist:
            stats.shortlisted += 1
        elif result.decision is Decision.consider:
            stats.considered += 1
        else:
            stats.skipped += 1
    session.flush()
    return stats
