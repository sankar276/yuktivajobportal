"""Read the facts out of a job description.

Postings bury the things that decide whether a role is even possible for you:
years asked for, a clearance, travel, whether sponsorship is on offer. These
are pulled out with conservative patterns, shown on the job card, usable as
filters, and (for clearance, sponsorship and travel) as skip rules.

A fact is only reported when the wording is explicit. Absence of a fact means
"the posting does not say", never "no".
"""

from __future__ import annotations

import re
from typing import Any

from jobportal.text import find_terms

_YEARS_RE = re.compile(
    r"(?<![\d.$])(\d{1,2})\s*(?:\+|plus\b|or more\b)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*"
    r"(?:years?|yrs?)\b['’]?\s*(?:of\s+)?(?:[a-z/,&\-]+\s+){0,5}?experience",
    re.IGNORECASE,
)
_MIN_YEARS_RE = re.compile(
    r"(?:minimum|min\.?|at least)\s+(?:of\s+)?(\d{1,2})\+?\s*(?:years?|yrs?)\b", re.IGNORECASE
)

_CLEARANCE_WORDS = (
    r"(?:security clearance|secret clearance|ts/sci|top secret|public trust|clearance)"
)
_CLEARANCE_OBTAIN_RE = re.compile(
    rf"\b(?:ability|able|eligible|eligibility|willing(?:ness)?)\s+to\s+(?:obtain|get|acquire)\b[^.\n]{{0,60}}{_CLEARANCE_WORDS}",
    re.IGNORECASE,
)
_CLEARANCE_REQUIRED_RE = re.compile(
    rf"\b(?:active|current|existing)\b[^.\n]{{0,40}}{_CLEARANCE_WORDS}"
    rf"|{_CLEARANCE_WORDS}\s+(?:is\s+)?(?:required|needed|mandatory)\b"
    rf"|\b(?:must|required to)\s+(?:have|hold|possess|maintain)\b[^.\n]{{0,50}}{_CLEARANCE_WORDS}"
    rf"|\brequires?\b[^.\n]{{0,40}}{_CLEARANCE_WORDS}",
    re.IGNORECASE,
)
_CLEARANCE_LEVELS = [
    ("TS/SCI", re.compile(r"\bts\s?/\s?sci\b", re.IGNORECASE)),
    ("Top Secret", re.compile(r"\btop secret\b", re.IGNORECASE)),
    (
        "Secret",
        re.compile(r"\bsecret clearance\b|\bsecret\b(?=[^.\n]{0,20}clearance)", re.IGNORECASE),
    ),
    ("Public Trust", re.compile(r"\bpublic trust\b", re.IGNORECASE)),
]

_NO_SPONSORSHIP_RE = re.compile(
    r"\b(?:unable|not able|cannot|can not|can't|will not|won't|do not|does not|don't|doesn't|no|not)\b"
    r"[^.\n]{0,60}\bsponsor(?:ship|ing)?\b"
    r"|\bwithout\b[^.\n]{0,30}\bsponsorship\b"
    r"|\bsponsorship\b[^.\n]{0,30}\b(?:is|are)\s+not\s+(?:available|offered|provided)\b",
    re.IGNORECASE,
)
_SPONSORSHIP_RE = re.compile(
    r"\bsponsorship\b[^.\n]{0,20}\b(?:is\s+)?(?:available|offered|provided)\b"
    r"|\b(?:we|will|can|do)\s+sponsor\b",
    re.IGNORECASE,
)

_TRAVEL_RE = re.compile(
    r"(\d{1,3})\s?%\s*(?:of\s+(?:the\s+)?time\s+)?(?:\w+\s+){0,3}?travel"
    r"|travel\b[^.%\n]{0,50}?(\d{1,3})\s?%",
    re.IGNORECASE,
)
_ONCALL_RE = re.compile(r"\bon[- ]call\b", re.IGNORECASE)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
_ONSITE_RE = re.compile(
    r"\b(?:on[- ]?site|in[- ]office|in[- ]person)\b[^.\n]{0,30}\b(?:\d\s+days|required|only|full[- ]time|role|position)\b"
    r"|\b(?:5|five)\s+days\b[^.\n]{0,20}\b(?:in|at)\s+(?:the\s+)?office\b",
    re.IGNORECASE,
)

_EDUCATION = [
    (
        "bachelor",
        re.compile(
            r"\bbachelor(?:'s|’s|s)?\b|\bB\.?S\.?\s*/\s*B\.?A\.?\b|\bB\.?[SA]\.?\s+degree\b|\bundergraduate degree\b",
            re.IGNORECASE,
        ),
    ),
    (
        "master",
        re.compile(
            r"\bmaster(?:'s|’s|s)?\b(?!\s+(?:data|branch|node|plan|schedule))|\bM\.S\.|\bMBA\b|\bM\.?S\.?\s+degree\b",
            re.IGNORECASE,
        ),
    ),
    ("phd", re.compile(r"\bph\.?\s?d\b|\bdoctorate\b|\bdoctoral\b", re.IGNORECASE)),
]

CERTIFICATIONS = (
    "CKA", "CKS", "CKAD", "CISSP", "CISM", "CISA", "CCSP", "PMP", "TOGAF", "ITIL", "CCNP", "CCIE",
    "Security+", "AWS Certified", "Azure Solutions Architect", "Google Cloud Professional",
    "Terraform Associate", "SAFe", "OSCP", "CEH",
)  # fmt: skip


def _years(text: str) -> int | None:
    found = [int(m.group(1)) for m in _YEARS_RE.finditer(text)]
    found += [int(m.group(1)) for m in _MIN_YEARS_RE.finditer(text)]
    plausible = [years for years in found if 1 <= years <= 30]
    # The most demanding figure is the role's real bar ("12+ years, 5+ leading teams").
    return max(plausible) if plausible else None


def _clearance(text: str) -> tuple[str | None, str | None]:
    obtainable = _CLEARANCE_OBTAIN_RE.search(text)
    remainder = _CLEARANCE_OBTAIN_RE.sub(
        " ", text
    )  # so "able to obtain" is not read as "must have"
    required = _CLEARANCE_REQUIRED_RE.search(remainder)
    if not required and not obtainable:
        return None, None
    level = next((name for name, pattern in _CLEARANCE_LEVELS if pattern.search(text)), None)
    return ("required" if required else "obtainable"), level


def _travel(text: str) -> int | None:
    values = [
        int(group)
        for match in _TRAVEL_RE.finditer(text)
        for group in match.groups()
        if group and int(group) <= 100
    ]
    return max(values) if values else None


def workplace_of(
    remote: bool | None, location: str, description: str, declared: str | None = None
) -> str | None:
    """``remote`` / ``hybrid`` / ``onsite``, from the most explicit signal available."""
    stated = (declared or "").strip().lower().replace("_", "-")
    if stated in ("remote", "hybrid"):
        return stated
    if stated in ("on-site", "onsite", "in-office"):
        return "onsite"
    if _HYBRID_RE.search(location):
        return "hybrid"
    if remote:
        return "remote"
    if _HYBRID_RE.search(description):
        return "hybrid"
    if remote is False or _ONSITE_RE.search(description):
        return "onsite"
    return None


def extract_facts(
    description: str,
    *,
    remote: bool | None = None,
    location: str = "",
    declared_workplace: str | None = None,
) -> dict[str, Any]:
    """Explicit facts in a posting. Keys are present only when the posting says so."""
    text = description or ""
    facts: dict[str, Any] = {}

    years = _years(text)
    if years is not None:
        facts["years_required"] = years

    clearance, level = _clearance(text)
    if clearance:
        facts["clearance"] = clearance
        if level:
            facts["clearance_level"] = level

    if _NO_SPONSORSHIP_RE.search(text):
        facts["sponsorship"] = "not_offered"
    elif _SPONSORSHIP_RE.search(text):
        facts["sponsorship"] = "offered"

    travel = _travel(text)
    if travel is not None:
        facts["travel_percent"] = travel

    levels = [name for name, pattern in _EDUCATION if pattern.search(text)]
    if levels:
        facts["education"] = levels[0]  # the lowest level named is the minimum asked for

    if _ONCALL_RE.search(text):
        facts["oncall"] = True

    workplace = workplace_of(remote, location, text, declared_workplace)
    if workplace:
        facts["workplace"] = workplace

    certifications = find_terms(text, CERTIFICATIONS)
    if certifications:
        facts["certifications"] = certifications
    return facts
