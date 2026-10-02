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
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.config import Employment, Lane, Profile, SearchConfig, Seniority
from jobportal.db import utcnow
from jobportal.models import Decision, Job, JobScore
from jobportal.sources.base import (
    MAX_LOCATION_CHARS,
    REMOTE_WORDS_RE,
    infer_remote,
    remote_unclear,
    segments,
)
from jobportal.text import (
    company_key,
    find_terms,
    phrase_in_title,
    sha256_text,
    states,
    title_words,
)

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
    facts: dict[str, Any]

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
    #: Something about the posting could not be read, so it waits for you
    #: instead of being shortlisted on its own.
    unsure: bool = False
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
}
_EXECUTIVE = {"cto", "cio", "ciso", "ceo", "coo", "cfo", "cpo", "svp", "evp", "president"}
#: Not yet a full-time hire, whatever else the title says.
_TRAINEES = {"intern", "internship", "apprentice", "trainee"}
_ENTRY_RE = re.compile(
    r"\b(?:co-?op|new grad(?:uate)?s?|early[- ]career|entry[- ]level)\b", re.IGNORECASE
)
#: Words that make a title an individual contributor's, whatever rank is attached.
_IC_ROLES = {
    "architect", "engineer", "developer", "scientist", "analyst", "administrator", "specialist",
    "consultant", "sre", "designer", "programmer", "technologist", "researcher", "technician",
}  # fmt: skip
_LEVEL_NUMBERS = {"1": Seniority.junior, "2": Seniority.mid, "3": Seniority.senior,
                  "4": Seniority.staff, "5": Seniority.principal}  # fmt: skip
# "Manager" in these titles names the job, not a team that reports to it.
_NOT_PEOPLE_MANAGERS = {
    "account", "project", "product", "program", "programme", "office", "case", "community",
    "marketing", "sales", "success", "relationship", "customer", "partner", "delivery",
    "portfolio", "campaign", "content", "brand", "social", "territory", "category", "practice",
    "property", "facilities", "event", "events",
}  # fmt: skip
# Where the role sits, not what level it is: "Architect, Office of the CTO".
_CONTEXT_RE = re.compile(
    r"\boffice of (?:the )?(?:cto|cio|ciso|ceo|coo|cfo|cpo)\b"
    r"|\b(?:cto|cio|ciso|ceo|coo|cfo|cpo)(?:['\u2019]s)? (?:org|organi[sz]ation|office|team|group|staff)\b"
    r"|\bmember of (?:the )?technical staff\b|\bchief of staff\b|\bsenior associate\b",
    re.IGNORECASE,
)
_FIELD_EXEC_RE = re.compile(r"\bfield (?:cto|ciso|cio)\b", re.IGNORECASE)
# "Head of Platform", "Head, Platform": runs a function. "Head Chef" does not.
_HEAD_OF_RE = re.compile(r"\bhead\s*(?:of\b|,)", re.IGNORECASE)


def infer_seniority(title: str) -> Seniority:
    """Read a level off a job title. Titles vary by company, so this is a best guess.

    Two things keep the guess honest. An individual contributor's title
    stays one whatever rank a bank appends to it ("Cloud Architect - AVP",
    "Platform Architect, Vice President"). And words that say where a role
    sits ("Office of the CTO", "CISO Org") are not read as its level.
    """
    if _FIELD_EXEC_RE.search(title):
        return Seniority.principal  # a senior customer-facing engineer, not the C-suite
    words = title_words(_CONTEXT_RE.sub(" ", title))
    present = set(words)
    if present & _TRAINEES or _ENTRY_RE.search(title):
        return Seniority.junior
    if present & _IC_ROLES:
        if "chief" in present:
            return Seniority.director  # "Chief Architect": a top engineer, not the C-suite
        level = _ladder(title, words)
        # At a bank a "Vice President" engineer is a senior one, not an officer.
        return max(level, Seniority.senior) if present & {"vp", "svp", "evp"} else level
    if present & _EXECUTIVE or "chief" in present:
        return Seniority.executive
    if "vp" in present:
        return Seniority.vp
    if "avp" in present:
        return Seniority.senior
    return _ladder(title, words)


def _ladder(title: str, words: list[str]) -> Seniority:
    """The level a title's own words give it, ranks of office aside."""
    present = set(words)
    senior = "senior" in present
    if "director" in present or _HEAD_OF_RE.search(title):
        return Seniority.director
    if present & {"principal", "distinguished", "fellow"}:
        return Seniority.principal
    if "manager" in present and not present & _NOT_PEOPLE_MANAGERS:
        return Seniority.principal if senior else Seniority.staff
    if "staff" in present or "lead" in present:
        return Seniority.principal if senior else Seniority.staff
    numbered = next((_LEVEL_NUMBERS[word] for word in words if word in _LEVEL_NUMBERS), None)
    if numbered is None and words[-1:] == ["i"]:
        numbered = Seniority.junior  # "Software Engineer I"
    if "architect" in present:
        if numbered is not None:
            return max(numbered, Seniority.senior)  # "Architect II"
        if present & _JUNIOR and not senior:
            return Seniority.mid
        return Seniority.principal if senior else Seniority.staff
    if numbered is not None:
        return max(numbered, Seniority.senior) if senior else numbered
    if senior:
        return Seniority.senior
    if present & _JUNIOR:
        return Seniority.junior
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
_STATE_CODES = frozenset(_US_STATES.values())
#: State codes that are also a country's code, or an ordinary word in capitals.
#: Standing alone ("CA, Remote", "Remote OR Hybrid") they are not read as states.
_UNSAFE_ALONE = frozenset(
    {"CA", "IN", "DE", "CO", "IL", "AR", "ID", "MA", "PA", "MT", "TN", "MD", "AL", "AZ", "LA",
     "ME", "MN", "OR", "OK", "HI"}
)  # fmt: skip
_BARE_STATE_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(sorted(_STATE_CODES - _UNSAFE_ALONE)) + r")(?![A-Za-z])"
)
#: A state name that, on its own, is more often the city.
_CITY_STATES = {"new york", "washington"}
_US_REGIONS = (
    "new england", "pacific northwest", "midwest", "east coast", "west coast", "bay area",
    "tri-state", "mountain west", "southeast", "southwest", "northeast", "mid-atlantic",
)  # fmt: skip
#: Large US cities that postings write without their state. Only names that
#: are not also a well-known place abroad.
_US_CITIES = (
    "new york city", "nyc", "san francisco", "south san francisco", "los angeles", "seattle",
    "boston", "chicago", "austin", "dallas", "houston", "denver", "atlanta", "miami",
    "philadelphia", "phoenix", "san diego", "minneapolis", "detroit", "pittsburgh", "nashville",
    "charlotte", "raleigh", "salt lake city", "las vegas", "sacramento", "palo alto",
    "mountain view", "sunnyvale", "santa clara", "menlo park", "redmond", "bellevue", "kirkland",
    "boulder", "cupertino", "irvine", "san antonio", "orlando", "tampa", "st louis", "st. louis",
    "saint louis", "kansas city", "indianapolis", "cincinnati", "cleveland", "columbus",
    "baltimore", "milwaukee", "oklahoma city", "new orleans", "honolulu", "fort worth", "plano",
    "frisco", "irving", "round rock", "reston", "mclean", "herndon", "tysons", "bethesda",
    "arlington", "ann arbor", "jersey city", "hoboken", "brooklyn", "manhattan", "oakland",
    "berkeley", "fremont", "redwood city", "san mateo", "santa monica", "pasadena", "long beach",
    "silicon valley", "scottsdale", "tempe", "chandler", "tucson", "albuquerque", "el paso",
    "boise", "omaha", "tulsa", "louisville", "memphis", "jacksonville", "fort lauderdale",
    "buffalo", "hartford", "stamford", "providence", "princeton", "wilmington", "alpharetta",
    "portland", "madison", "provo", "lehi", "spokane", "tacoma", "des moines",
)  # fmt: skip
_US_CITY_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(re.escape(city) for city in sorted(_US_CITIES, key=len, reverse=True))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)
# The country itself, named outright, or a region it is part of ("North
# America", "Americas"). "US" only in capitals ("us" is a word).
_US_COUNTRY_RE = re.compile(
    r"(?<![A-Za-z])(?:US|USA|AMER)(?![A-Za-z])"
    r"|(?i:\bunited states(?: of america)?\b|\bu\.s(?:\.a)?\.?(?![A-Za-z])|\bamericas\b|\bnoram\b"
    r"|\bamerica\b(?<!latin america)(?<!south america)(?<!central america))"
)
_US_STATE_NAME_RE = re.compile(
    r"\b(?:state of\s+)?("
    + "|".join(re.escape(n) for n in sorted([*_US_STATES, *_US_REGIONS], key=len, reverse=True))
    + r")(?:\s+state)?\b",
    re.IGNORECASE,
)
# "City, ST": a place name, a comma, a two-letter code in capitals.
_CITY_CODE_RE = re.compile(r"([A-Za-z][A-Za-z .'\-]*?),\s*([A-Z]{2})(?![A-Za-z])")
#: Cities outside the US whose "City, XX" form collides with a US state code,
#: or whose name is also a US town, with the country they are in.
_FOREIGN_CITIES: dict[str, set[str]] = {
    "toronto": {"CA", "ON"}, "vancouver": {"CA", "BC"}, "montreal": {"CA", "QC"},
    "ottawa": {"CA", "ON"}, "calgary": {"CA", "AB"}, "edmonton": {"CA", "AB"},
    "winnipeg": {"CA", "MB"}, "halifax": {"CA", "NS"}, "waterloo": {"CA", "ON"},
    "bangalore": {"IN"}, "bengaluru": {"IN"}, "hyderabad": {"IN"}, "pune": {"IN"},
    "chennai": {"IN"}, "mumbai": {"IN"}, "gurgaon": {"IN"}, "gurugram": {"IN"}, "noida": {"IN"},
    "delhi": {"IN"}, "new delhi": {"IN"}, "kolkata": {"IN"}, "ahmedabad": {"IN"},
    "berlin": {"DE"}, "munich": {"DE"}, "hamburg": {"DE"}, "frankfurt": {"DE"},
    "cologne": {"DE"}, "stuttgart": {"DE"}, "dusseldorf": {"DE"},
    "bogota": {"CO"}, "medellin": {"CO"}, "cali": {"CO"},
    "tel aviv": {"IL"}, "haifa": {"IL"}, "jerusalem": {"IL"}, "herzliya": {"IL"},
    "tbilisi": {"GE", "GEORGIA"}, "batumi": {"GE", "GEORGIA"},
    "buenos aires": {"AR"}, "cordoba": {"AR"}, "jakarta": {"ID"}, "casablanca": {"MA"},
    "panama city": {"PA"}, "tunis": {"TN"}, "vientiane": {"LA"}, "baku": {"AZ"},
    "tirana": {"AL"}, "chisinau": {"MD"}, "ulaanbaatar": {"MN"}, "valletta": {"MT"},
}  # fmt: skip
_ELSEWHERE = [
    "Canada", "Mexico", "Brazil", "Argentina", "Colombia", "Chile", "Peru", "Costa Rica",
    "Uruguay", "Ecuador", "Venezuela", "Bolivia", "Guatemala", "Panama", "LATAM",
    "Latin America", "South America", "Central America", "UK", "United Kingdom",
    "Great Britain", "England", "Scotland", "Wales", "Ireland", "Germany", "France", "Spain",
    "Portugal", "Italy", "Netherlands", "Belgium", "Luxembourg", "Poland", "Romania", "Sweden",
    "Norway", "Denmark", "Finland", "Iceland", "Switzerland", "Austria", "Czech", "Czechia",
    "Slovakia", "Slovenia", "Hungary", "Bulgaria", "Serbia", "Croatia", "Greece", "Cyprus",
    "Malta", "Estonia", "Lithuania", "Latvia", "Ukraine", "Russia", "Belarus", "Kazakhstan",
    "Armenia", "Turkey", "Israel", "UAE", "United Arab Emirates", "Saudi Arabia", "Qatar",
    "Bahrain", "Kuwait", "India", "Pakistan", "Sri Lanka", "Bangladesh", "Nepal", "Singapore",
    "Japan", "China", "Hong Kong", "Taiwan", "Korea", "Philippines", "Vietnam", "Indonesia",
    "Malaysia", "Thailand", "Cambodia", "Australia", "New Zealand", "South Africa", "Nigeria",
    "Kenya", "Ghana", "Ethiopia", "Egypt", "Morocco", "Tunisia", "Europe", "European Union",
    "EU", "EMEA", "APAC", "APJ", "Asia", "Africa", "Middle East", "London", "Manchester",
    "Edinburgh", "Glasgow", "Belfast", "Dublin", "Cork", "Berlin", "Munich", "Hamburg",
    "Frankfurt", "Paris", "Amsterdam", "Rotterdam", "Brussels", "Madrid", "Barcelona",
    "Lisbon", "Porto", "Rome", "Milan", "Warsaw", "Krakow", "Wroclaw", "Gdansk", "Prague",
    "Vienna", "Budapest", "Bucharest", "Sofia", "Belgrade", "Zagreb", "Athens", "Tallinn",
    "Vilnius", "Riga", "Kyiv", "Istanbul", "Ankara", "Copenhagen", "Oslo", "Stockholm",
    "Helsinki", "Zurich", "Geneva", "Toronto", "Vancouver", "Montreal", "Ottawa", "Calgary",
    "Edmonton", "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba", "Nova Scotia",
    "Saskatchewan", "Bangalore", "Bengaluru", "Hyderabad", "Pune", "Chennai", "Mumbai",
    "Gurgaon", "Gurugram", "Noida", "Delhi", "Kolkata", "Karachi", "Lahore", "Dhaka", "Colombo",
    "Manila", "Cebu", "Kuala Lumpur", "Jakarta", "Bangkok", "Hanoi", "Ho Chi Minh", "Seoul",
    "Shanghai", "Beijing", "Shenzhen", "Taipei", "Tokyo", "Osaka", "Sydney", "Melbourne",
    "Brisbane", "Perth", "Auckland", "Wellington", "Tel Aviv", "Haifa", "Dubai", "Abu Dhabi",
    "Riyadh", "Doha", "Cairo", "Lagos", "Nairobi", "Cape Town", "Johannesburg", "Sao Paulo",
    "Rio de Janeiro", "Buenos Aires", "Bogota", "Medellin", "Lima", "Santiago", "Mexico City",
    "Guadalajara", "Monterrey", "Tbilisi", "Dominican Republic", "Jamaica", "El Salvador",
    "Paraguay", "Honduras", "Nicaragua", "Cuba", "Haiti", "Trinidad", "Bahamas", "Barbados",
    "Belize", "Guyana", "Caribbean", "Albania", "Andorra", "Bosnia", "Kosovo", "Liechtenstein",
    "Moldova", "Monaco", "Montenegro", "Macedonia", "Turkiye", "Azerbaijan", "Uzbekistan",
    "Kyrgyzstan", "Mongolia", "Myanmar", "Laos", "Brunei", "Iran", "Iraq", "Jordan", "Lebanon",
    "Oman", "Algeria", "Angola", "Botswana", "Cameroon", "Ivory Coast", "Senegal", "Tanzania",
    "Uganda", "Rwanda", "Zambia", "Zimbabwe", "Mauritius", "Namibia", "Mozambique", "Fiji",
    "Oceania", "Scandinavia", "Nordics", "Nordic", "Benelux", "Balkans", "Baltics", "Iberia",
    "DACH", "CEE", "MENA", "ANZ",
]  # fmt: skip
_ELSEWHERE_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(re.escape(place) for place in sorted(_ELSEWHERE, key=len, reverse=True))
    # Workday writes Canada as "CA, ON, Toronto": country code, then province.
    + r"|CA,\s*(?:ON|BC|QC|AB|MB|SK|NS|NB)"
    + r"|Georgia\s*\((?:the\s+)?country\)"
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)
# Words in a location that name no place at all. The joining words only in
# lower case: "IN" and "OR" in capitals may be a country or a state.
_NO_PLACE_RE = re.compile(
    r"(?i:\b(?:multiple|various|several|many|all|other) (?:locations?|sites|offices|cities|countries)\b"
    r"|\b\d+ locations?\b|\bglobal(?:ly)?\b|\bworldwide\b|\binternational\b|\bflexible\b"
    r"|\bto be determined\b|\btbd\b|\bn/?a\b|\bany(?: ?where)?\b"
    r"|\b(?:hybrid|on[- ]?site|in[- ]office|in[- ]person|optional|eligible|friendly|first|fully"
    r"|only|based|role|position|options?|available|preferred|locations?|office|distributed|open"
    r"|work|from|home|field)\b)"
    r"|\b(?:and|or|the|in|within|of|to)\b"
)


class LocationFit(NamedTuple):
    value: float  # 0..1
    note: str
    hard_fail: bool
    #: The place could not be read. The job is scored, but a person should
    #: look before anything is sent, so it is never shortlisted on its own.
    unsure: bool = False


class _UsEvidence(NamedTuple):
    country: bool  # the country, named outright
    state: bool  # a state or region, named on its own
    city: bool  # "City, ST", or a large city written without its state
    rest: str  # the text with everything read as American taken out

    @property
    def found(self) -> bool:
        return self.country or self.state or self.city


def _fold(text: str) -> str:
    """Accents removed, so "Bogot\u00e1" and "Krak\u00f3w" meet the lists above."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _us_evidence(segment: str) -> _UsEvidence:
    """What in one place string says "United States".

    Everything read as American is taken out of ``rest``, so that "New
    Mexico", "Dublin, OH" and "Vancouver, WA" are not then found on the list
    of places abroad. A two-letter code counts as a state in "City, ST" form,
    unless the city is a known one abroad whose country has that code
    ("Gurugram, IN", "Hamburg, DE"); standing alone it counts only when it
    cannot be a country's code instead ("TX, Remote", but not "CA, Remote").
    """
    text = _fold(segment[:MAX_LOCATION_CHARS])
    country = bool(_US_COUNTRY_RE.search(text))
    rest = _US_COUNTRY_RE.sub(" ", text)
    seen = {"state": False, "city": False}

    def city_code(match: re.Match[str]) -> str:
        code = match.group(2)
        # "Remote - Austin, TX": the city is what follows the last dash.
        city = re.split(r"\s[-\u2013]\s|[:(]", match.group(1))[-1].strip().lower()
        if code not in _STATE_CODES or code in _FOREIGN_CITIES.get(city, ()):
            return match.group(0)
        if not city or REMOTE_WORDS_RE.fullmatch(city) or city.upper() in _STATE_CODES:
            return match.group(0)  # "Remote, CA": no city in front of the code
        seen["city"] = True
        return " "

    rest = _CITY_CODE_RE.sub(city_code, rest)

    def state_name(match: re.Match[str]) -> str:
        before = rest[: match.start()].rstrip(" ,").lower()
        city = re.split(r"[,;]|\s[-\u2013]\s", before)[-1].strip()
        name = match.group(1).lower()
        if name.upper() in _FOREIGN_CITIES.get(city, ()):
            return match.group(0)  # "Tbilisi, Georgia" is the country
        if name == "georgia" and re.match(r"\s*\((?:the\s+)?country\)", rest[match.end() :], re.I):
            return match.group(0)
        alone = match.group(0).lower() == name
        seen["city" if alone and name in _CITY_STATES else "state"] = True
        return " "

    rest = _US_STATE_NAME_RE.sub(state_name, rest)
    if _BARE_STATE_RE.search(rest):
        seen["state"] = True
        rest = _BARE_STATE_RE.sub(" ", rest)
    if _US_CITY_RE.search(rest):
        seen["city"] = True
        rest = _US_CITY_RE.sub(" ", rest)
    return _UsEvidence(country, seen["state"], seen["city"], rest)


def mentions_us(text: str) -> bool:
    """Does a place string name the United States, one of its states or cities?"""
    return _us_evidence(text).found


def _with_state_codes(text: str) -> str:
    """State names as their codes, so "Austin, Texas" and "Austin, TX" compare equal."""
    return _US_STATE_NAME_RE.sub(
        lambda m: _US_STATES.get(m.group(1).lower(), m.group(0)), _fold(text)
    )


def _place_in(place: str, text: str) -> bool:
    """Whole-word match; short all-caps places ("TX", "US") are case-sensitive."""
    place, text = _with_state_codes(place), _with_state_codes(text)
    flags = 0 if (len(place) <= 3 and place.isupper()) else re.IGNORECASE
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(place)}(?![A-Za-z])", text, flags))


def _is_onsite(place: str, text: str) -> bool:
    """Is ``place``, one you would work on-site in, named by this location?

    A place that is a US state ("CA", "Indiana") counts only where the
    location means the state: "Toronto, CA" and "Bangalore, IN" do not.
    """
    if not _place_in(place, text):
        return False
    if _with_state_codes(place).strip() not in _STATE_CODES:
        return True
    return any(_place_in(place, part) and mentions_us(part) for part in _segments(text))


def _wants_us(regions: list[str]) -> bool:
    return any(region.strip().lower() in _US_NAMES for region in regions)


def _segments(text: str) -> list[str]:
    """A posting often lists several places; each is judged on its own."""
    return segments(text)


def _foreign_city(text: str) -> bool:
    return any(
        match.group(2) in _FOREIGN_CITIES.get(match.group(1).strip().lower(), ())
        for match in _CITY_CODE_RE.finditer(text)
    )


def _abroad(text: str) -> bool:
    """Does ``text`` (already folded) name a place outside the United States?"""
    return bool(_ELSEWHERE_RE.search(text)) or _foreign_city(text)


def _named_place(text: str) -> str:
    """What is left of a location once the words that name no place are removed."""
    rest = _NO_PLACE_RE.sub(" ", REMOTE_WORDS_RE.sub(" ", text))
    return re.sub(r"[^A-Za-z]+", " ", rest).strip()


def _region_fit(segment: str, regions: list[str]) -> str:
    """Is this place inside the regions you accept?

    ``yes`` or ``no`` when the place could be read; ``unstated`` when the
    segment names no place at all ("Remote"); ``unknown`` when it names one
    that could not be placed. A region you accept wins over any other named
    next to it: "US & Canada" is open to the US.
    """
    us = _us_evidence(segment)
    if us.found and _wants_us(regions):
        return "yes"
    if any(_place_in(region, segment) for region in regions):
        return "yes"
    if us.found or _abroad(us.rest):
        return "no"
    return "unknown" if _named_place(us.rest) else "unstated"


def _is_countrywide(segment: str, regions: list[str]) -> bool:
    """The whole location is a country, state or region you accept, with no city in it.

    "United States", "Texas, United States", "California": usually remote
    within that area rather than a desk in a particular town.
    """
    names = {region.strip().lower() for region in regions}
    if re.sub(r"[^a-z. ]+", " ", segment.lower()).strip() in names:
        return True
    if not _wants_us(regions):
        return False
    us = _us_evidence(segment)
    return (us.country or us.state) and not us.city and not _named_place(us.rest)


def judge_location(job: Scorable, lane: Lane) -> LocationFit:
    """How the job's location fits the lane.

    Only a place that was recognised, and is not one of yours, is a reason to
    skip. A place that could not be read is scored as unknown and marked
    ``unsure``, which keeps the job off the shortlist until you have looked.
    """
    rules = lane.locations
    text = (job.location or "")[:MAX_LOCATION_CHARS]
    shown = f" ({text})" if text else ""
    onsite_hit = next((place for place in rules.onsite if _is_onsite(place, text)), None)
    remote = job.remote
    if remote is None and infer_remote(text):
        remote = True  # "Virtual - US", "Home Based", "Nationwide"

    if remote:
        if not rules.remote:
            if onsite_hit:
                return LocationFit(1.0, f"Remote role, and {onsite_hit} is on your list", False)
            return LocationFit(0.0, "Remote role; this lane is on-site only", True)
        if not rules.remote_regions:
            return LocationFit(1.0, f"Remote{shown}", False)
        fits = [_region_fit(segment, rules.remote_regions) for segment in _segments(text)]
        if "yes" in fits:
            return LocationFit(1.0, f"Remote{shown}", False)
        if onsite_hit:
            return LocationFit(1.0, f"Remote or {onsite_hit}{shown}", False)
        if not fits or "unstated" in fits:
            return LocationFit(0.9, f"Remote; region not stated{shown}", False)
        if "unknown" in fits:
            return LocationFit(0.5, f"Remote; could not tell from where{shown}", False, True)
        return LocationFit(0.0, f"Remote, but outside your regions{shown}", True)

    if onsite_hit:
        kind = "On-site or hybrid" if job.remote is False else "Based"
        return LocationFit(1.0, f"{kind} in {onsite_hit}{shown}", False)
    if job.remote is False and rules.remote and not rules.onsite:
        return LocationFit(0.0, f"On-site or hybrid; this lane is remote only{shown}", True)
    if not _named_place(text):
        return LocationFit(0.5, f"Location not stated{shown}", False)
    if job.remote is None and rules.remote and remote_unclear(text):
        # "Hybrid (2 days remote)", "Hybrid/Remote - NYC": not for this app to decide.
        note = f"Says both remote and on-site or hybrid; could not tell which{shown}"
        return LocationFit(0.5, note, False, True)
    if (
        job.remote is None
        and rules.remote
        and any(_is_countrywide(segment, rules.remote_regions) for segment in _segments(text))
    ):
        # "United States" with no city usually means remote within the country.
        return LocationFit(0.7, f"Country-wide location, likely remote{shown}", False)
    if not rules.onsite and not rules.remote:
        return LocationFit(0.5, f"No location rules set for this lane{shown}", False)
    if not (mentions_us(text) or _abroad(_fold(text))):
        return LocationFit(0.5, f"Could not tell where this is{shown}", False, True)
    wanted = "remote" if rules.remote and not rules.onsite else "in your locations"
    return LocationFit(0.0, f"Not {wanted}: {text}", True)


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
    currency = (job.comp_currency or rules.currency).upper()
    low = f"{job.comp_min:,.0f}" if job.comp_min is not None else "?"
    shown = f"{currency} {low}-{job.comp_max:,.0f} per {job.comp_period}"
    if currency != rules.currency.upper():
        return f"Advertised pay: {shown}", False  # another currency: never compared
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
    location = judge_location(job, lane)
    result.unsure = location.unsure
    checks = (
        ("title", weights.title, _title_factor(job, lane)),
        ("seniority", weights.seniority, _seniority_factor(job, lane)),
        ("location", weights.location, location[:3]),
        ("skills", weights.skills, _skills_factor(job, lane, result)),
    )
    for name, weight, (value, note, hard_fail) in checks:
        if hard_fail:
            result.skip = note
            return result
        result.factors.append(Factor(name, value, weight, note))

    if not job.needs_detail:
        description = job.description_text or ""
        phrase = next((p for p in lane.skip_if_description_has if states(description, p)), None)
        if phrase:
            result.skip = f"Posting says '{phrase}'"
            return result

    travel = (job.facts or {}).get("travel_percent")
    if (
        lane.max_travel_percent is not None
        and travel is not None
        and travel > lane.max_travel_percent
    ):
        result.skip = (
            f"States {travel}% travel; your limit for this lane is {lane.max_travel_percent}%"
        )
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
    """The blocklist entry that matches this company, if any.

    An entry matches a longer name that contains it ("Northwind" blocks
    "Northwind Systems Europe"), and a shorter name it starts with: a board
    added without a company name is known only as "northwind". Blocking one
    company too many is the safe mistake for a list of places never to apply.
    """
    key = company_key(company_name)
    if not key:
        return None
    for entry in blocked:
        entry_key = company_key(entry)
        if not entry_key:
            continue
        if entry_key == key or re.search(rf"\b{re.escape(entry_key)}\b", key):
            return entry
        if len(key) >= 4 and entry_key.startswith(key + " "):
            return entry
    return None


#: Clearances from least to most, under the names the fact extractor uses.
_CLEARANCE_RANK = {"public trust": 0, "confidential": 1, "secret": 2, "top secret": 3, "ts/sci": 4}
_HELD_CLEARANCE = (
    (4, re.compile(r"\bts ?/ ?sci\b|\bsci\b", re.IGNORECASE)),
    (3, re.compile(r"\btop secret\b|\bts\b", re.IGNORECASE)),
    (2, re.compile(r"\bsecret\b", re.IGNORECASE)),
    (1, re.compile(r"\bconfidential\b", re.IGNORECASE)),
    (0, re.compile(r"\bpublic trust\b", re.IGNORECASE)),
)


def clearance_check(job: Scorable, profile: Profile | None) -> tuple[str, bool]:
    """``(note, blocks)`` for a posting that requires an active security clearance.

    Blocks when you hold none, or hold one that is clearly below what is
    asked. When the two cannot be compared (a clearance this does not know,
    such as one from another country) the note asks you to check, and the
    posting is kept off the shortlist rather than guessed at either way.
    """
    facts = job.facts or {}
    if profile is None or facts.get("clearance") != "required":
        return "", False
    level = facts.get("clearance_level") or ""
    wanted = f"an active {level + ' ' if level else ''}security clearance"
    held = profile.security_clearance
    if not held:
        return f"Requires {wanted}", True
    have = next((rank for rank, pattern in _HELD_CLEARANCE if pattern.search(held)), None)
    if have is None:
        return f"Requires {wanted}; check that against yours ({held})", False
    # With no level named, anything that is a clearance proper will do.
    need = _CLEARANCE_RANK.get(level.lower(), 1)
    if need > have:
        return f"Requires {wanted}; your profile says {held}", True
    return "", False


def profile_block(job: Scorable, profile: Profile | None) -> str | None:
    """A stated requirement of the posting that you cannot meet, whatever the lane."""
    if profile is None:
        return None
    note, blocks = clearance_check(job, profile)
    if blocks:
        return note
    facts = job.facts or {}
    if facts.get("sponsorship") == "not_offered" and profile.work_authorization.needs_sponsorship:
        return "Posting says visa sponsorship is not available"
    return None


def score_job(
    job: Scorable,
    search: SearchConfig,
    *,
    now: datetime | None = None,
    profile: Profile | None = None,
) -> ScoreResult:
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
    cannot_meet = profile_block(job, profile)
    if cannot_meet:
        return ScoreResult(
            lane=None,
            score=0.0,
            decision=Decision.skip,
            reasons=[cannot_meet],
            breakdown={"requirement": cannot_meet},
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

    bars = {lane.key: lane.shortlist_at for lane in search.lanes}
    check_note, _blocks = clearance_check(job, profile)

    def clears(result: LaneResult) -> bool:
        return (
            result.score >= bars[result.lane]
            and not result.provisional
            and not result.unsure
            and not check_note
        )

    # A lane whose own bar the posting clears is preferred to one that merely
    # scores a little higher against a bar it misses.
    best = max(scored, key=lambda r: (clears(r), r.score))
    lane = search.lane(best.lane)
    assert lane is not None
    shortlisted = clears(best)
    total = lane.weights.total()
    reasons = [f.note for f in sorted(best.factors, key=lambda f: f.points, reverse=True)]
    reasons += best.notes
    if check_note:
        reasons.append(check_note)
    asked = (job.facts or {}).get("years_required")
    have = profile.years_experience if profile is not None else None
    if asked and have is not None and asked > have:
        reasons.append(f"Asks for {asked}+ years; your profile says {have}")
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
                    # Both on the 0-100 scale of the score, whatever the weights add up to.
                    "weight": round(100 * f.weight / total, 1),
                    "points": round(100 * f.points / total, 1),
                    "note": f.note,
                }
                for f in best.factors
            ],
            "core_found": best.core_found,
            "bonus_found": best.bonus_found,
            "provisional": best.provisional,
            "unsure": best.unsure or bool(check_note),
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


def search_terms(search: SearchConfig) -> tuple[str, ...]:
    """Every title you named, used to query sources that are searched rather than listed.

    Target titles of all lanes come first, then related ones, so a source
    that only takes so many searches spends them on what you want most.
    """
    terms: dict[str, None] = {}
    for kind in ("target", "related"):
        for lane in search.lanes:
            for phrase in getattr(lane.titles, kind):
                if phrase.strip():
                    terms.setdefault(phrase.strip().lower())
    return tuple(terms)


# -------------------------------------------------------------- persistence


@dataclass
class ScoreStats:
    scored: int = 0
    shortlisted: int = 0
    considered: int = 0
    skipped: int = 0
    unchanged: int = 0


def rubric_hash(search: SearchConfig, profile: Profile | None = None) -> str:
    payload = search.model_dump(mode="json", exclude={"policy": {"mode", "auto", "email"}})
    if profile is not None:
        payload["profile"] = {
            "clearance": profile.security_clearance,
            "needs_sponsorship": profile.work_authorization.needs_sponsorship,
            "years": profile.years_experience,
        }
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
        json.dumps(job.facts or {}, sort_keys=True),
    )


def score_jobs(
    session: Session,
    user_id: int,
    search: SearchConfig,
    *,
    now: datetime | None = None,
    job_ids: list[int] | None = None,
    force: bool = False,
    profile: Profile | None = None,
) -> ScoreStats:
    """Score open jobs that are new, changed, or whose score has gone stale."""
    now = now or utcnow()
    rubric = rubric_hash(search, profile)
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
        result = score_job(job, search, now=now, profile=profile)
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
