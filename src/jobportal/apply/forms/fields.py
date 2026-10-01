"""Understand application-form fields and decide what goes in each.

The rule throughout: an answer is either something you configured, something
you answered before, or it is missing. Nothing is guessed. Questions with
legal weight (work authorisation, sponsorship) are only answered from your
profile when their wording is the plain, standard one; any unusual phrasing
is handed to you instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from jobportal.config import Profile
from jobportal.scoring import _ELSEWHERE_RE, mentions_us
from jobportal.text import question_key, squash


class FieldKind(StrEnum):
    first_name = "first_name"
    last_name = "last_name"
    full_name = "full_name"
    preferred_name = "preferred_name"
    email = "email"
    phone = "phone"
    resume = "resume"
    cover_letter = "cover_letter"
    linkedin = "linkedin"
    github = "github"
    website = "website"
    location = "location"
    city = "city"
    region = "region"
    country = "country"
    postal_code = "postal_code"
    current_company = "current_company"
    current_title = "current_title"
    years_experience = "years_experience"
    work_authorized = "work_authorized"
    needs_sponsorship = "needs_sponsorship"
    eeo_gender = "eeo_gender"
    eeo_race = "eeo_race"
    eeo_veteran = "eeo_veteran"
    eeo_disability = "eeo_disability"
    question = "question"  # anything else: answered from your answer bank


#: Control types as reported by the page scan.
TEXT_TYPES = {"text", "email", "tel", "url", "number", "textarea", "date"}
CHOICE_TYPES = {"select", "radio", "checkbox", "combobox"}

DECLINE = "decline"
_DECLINE_RE = re.compile(
    r"decline|prefer not|rather not|choose not|do(?:n't| not) wish|not wish to|"
    r"do(?:n't| not) want to (?:answer|say|disclose)|not to (?:answer|say|disclose|self)",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"\b(without|unable|not|don't|never|except|unless)\b", re.IGNORECASE)


@dataclass
class FormField:
    """One control (or one radio/checkbox group) found on the page."""

    ref: str  # selector the filler uses to find it again
    label: str
    type: str  # text, email, tel, url, number, date, textarea, select, radio, checkbox, file, combobox
    required: bool = False
    name: str = ""
    #: For choices: ``[{"label": ..., "ref": ...}]``.
    options: list[dict[str, str]] = field(default_factory=list)
    #: Something is already entered (the site pre-filled it).
    prefilled: bool = False

    @property
    def key(self) -> str:
        return question_key(self.label) or question_key(self.name)

    @property
    def option_labels(self) -> list[str]:
        return [option["label"] for option in self.options]


@dataclass
class Resolution:
    """What to put in a field, and where that came from."""

    value: str
    source: str  # "profile", "answer bank", "standard answer"
    #: For choices: the option to pick.
    option: dict[str, str] | None = None


# ----------------------------------------------------------- classification

# Checked in order: specific patterns before general ones.
_LABEL_RULES: list[tuple[FieldKind, re.Pattern[str]]] = [
    (FieldKind.resume, re.compile(r"\b(resume|résumé|cv|curriculum vitae)\b", re.I)),
    (FieldKind.cover_letter, re.compile(r"\bcover letter\b", re.I)),
    (FieldKind.linkedin, re.compile(r"linked\s?in", re.I)),
    (FieldKind.github, re.compile(r"\bgit\s?hub\b", re.I)),
    (FieldKind.website, re.compile(r"\b(website|portfolio|personal site|blog)\b", re.I)),
    (
        FieldKind.preferred_name,
        re.compile(r"\b(preferred|nick)\s?(first )?name\b|\bgoes by\b", re.I),
    ),
    (FieldKind.first_name, re.compile(r"\b(first|given)\s?name\b", re.I)),
    (FieldKind.last_name, re.compile(r"\b(last|family)\s?name\b|\bsurname\b", re.I)),
    (
        FieldKind.current_company,
        re.compile(
            r"\b(current|present|most recent)\s+(company|employer)\b|^(company|employer|organization|org)( name)?$",
            re.I,
        ),
    ),
    (
        FieldKind.current_title,
        re.compile(
            r"\b(current|present|most recent)\s+(job )?(title|role|position)\b|^(job )?title$", re.I
        ),
    ),
    (
        FieldKind.full_name,
        re.compile(r"^(full |legal |your )?name$|\bfull name\b|\blegal name\b", re.I),
    ),
    (FieldKind.email, re.compile(r"\be-?mail\b", re.I)),
    (FieldKind.phone, re.compile(r"\b(phone|mobile|telephone|cell)\b", re.I)),
    (
        FieldKind.location,
        re.compile(
            r"^(current )?location\b|\bwhere are you (currently )?(located|based)\b|\bcity\b.*\bstate\b",
            re.I,
        ),
    ),
    (FieldKind.postal_code, re.compile(r"\b(zip|postal)\s?(code)?\b", re.I)),
    (FieldKind.city, re.compile(r"^city$|\bcity\b(?!.*\bstate\b)", re.I)),
    (FieldKind.region, re.compile(r"^(state|province|region)$|\bstate\s?/\s?province\b", re.I)),
    (FieldKind.country, re.compile(r"^country$|\bcountry of residence\b", re.I)),
    (
        FieldKind.years_experience,
        re.compile(r"\b(total )?years of (professional |relevant |work )?experience\b", re.I),
    ),
]
_NAME_HINTS: dict[str, FieldKind] = {
    "first_name": FieldKind.first_name,
    "firstname": FieldKind.first_name,
    "last_name": FieldKind.last_name,
    "lastname": FieldKind.last_name,
    "name": FieldKind.full_name,
    "email": FieldKind.email,
    "phone": FieldKind.phone,
    "resume": FieldKind.resume,
    "org": FieldKind.current_company,
    "urls[linkedin]": FieldKind.linkedin,
    "urls[github]": FieldKind.github,
    "urls[portfolio]": FieldKind.website,
    "_systemfield_name": FieldKind.full_name,
    "_systemfield_email": FieldKind.email,
    "_systemfield_resume": FieldKind.resume,
}
_AUTHORIZED_RE = re.compile(
    r"\b(authori[sz]ed|eligible|entitled|permitted|allowed|have the (legal )?right)\b.{0,40}\bto work\b",
    re.IGNORECASE,
)
_SPONSORSHIP_RE = re.compile(
    r"\b(require|need)\b.{0,60}\bsponsor(ship)?\b|\bsponsor(ship)?\b.{0,40}\b(required|needed)\b",
    re.IGNORECASE,
)


def classify(form_field: FormField, profile: Profile) -> FieldKind:
    """What a field is asking for, judged from its label, name and type."""
    label = squash(form_field.label)
    lowered_name = form_field.name.strip().lower()

    if form_field.type == "file":
        if (
            FieldKind.cover_letter.value.replace("_", " ") in label.lower()
            or "cover" in lowered_name
        ):
            return FieldKind.cover_letter
        return (
            FieldKind.resume
            if re.search(r"resume|résumé|cv\b", f"{label} {lowered_name}", re.I) or not label
            else FieldKind.question
        )

    if form_field.type in CHOICE_TYPES or form_field.options:
        if re.search(r"\bgender\b", label, re.I):
            return FieldKind.eeo_gender
        if re.search(r"\b(race|ethnicity|ethnic|hispanic|latino)\b", label, re.I):
            return FieldKind.eeo_race
        if re.search(r"\bveteran\b", label, re.I):
            return FieldKind.eeo_veteran
        if re.search(r"\bdisabilit", label, re.I):
            return FieldKind.eeo_disability

    legal = _legal_kind(label, profile)
    if legal is not None:
        return legal
    if (
        _AUTHORIZED_RE.search(label)
        or re.search(r"\bsponsor", label, re.I)
        or re.search(r"\bvisa\b", label, re.I)
    ):
        return FieldKind.question  # legal wording we do not recognise exactly: ask you

    if form_field.type == "email":
        return FieldKind.email
    if form_field.type == "tel":
        return FieldKind.phone
    # A long question that merely mentions a field word ("What is your company's
    # biggest challenge?") is a question, not that field.
    if len(label) <= 60:
        for kind, pattern in _LABEL_RULES:
            if pattern.search(label):
                return kind
    if lowered_name in _NAME_HINTS and len(label) <= 60:
        return _NAME_HINTS[lowered_name]
    return FieldKind.question


def _legal_kind(label: str, profile: Profile) -> FieldKind | None:
    """Recognise only the plain, standard wording of the two legal questions."""
    if _NEGATION_RE.search(label) or len(label) > 220:
        return None
    country = profile.work_authorization.country.strip().lower()
    is_us = country in {"united states", "us", "usa", "united states of america"}
    names_country = mentions_us(label) if is_us else bool(country and country in label.lower())
    if not names_country or (is_us and _ELSEWHERE_RE.search(label)):
        return None
    authorized = bool(_AUTHORIZED_RE.search(label))
    sponsorship = bool(_SPONSORSHIP_RE.search(label))
    if authorized and not sponsorship:
        return FieldKind.work_authorized
    if sponsorship and not authorized:
        return FieldKind.needs_sponsorship
    return None  # both at once ("authorized ... without sponsorship") is ambiguous


# ------------------------------------------------------------------ answers


def profile_value(kind: FieldKind, profile: Profile) -> str | None:
    """The profile's answer for a recognised field, or ``None`` when it has none."""
    auth = profile.work_authorization
    values: dict[FieldKind, str | None] = {
        FieldKind.first_name: profile.first_name,
        FieldKind.last_name: profile.last_name,
        FieldKind.full_name: profile.name,
        FieldKind.preferred_name: profile.preferred_name or profile.first_name,
        FieldKind.email: profile.email,
        FieldKind.phone: profile.phone,
        FieldKind.linkedin: profile.links.get("linkedin"),
        FieldKind.github: profile.links.get("github"),
        FieldKind.website: profile.links.get("website") or profile.links.get("portfolio"),
        FieldKind.location: profile.location.display(),
        FieldKind.city: profile.location.city,
        FieldKind.region: profile.location.region,
        FieldKind.country: profile.location.country,
        FieldKind.postal_code: profile.location.postal_code,
        FieldKind.current_company: profile.current_company,
        FieldKind.current_title: profile.current_title,
        FieldKind.years_experience: (
            str(profile.years_experience) if profile.years_experience is not None else None
        ),
        FieldKind.work_authorized: _yes_no(auth.authorized),
        FieldKind.needs_sponsorship: _yes_no(auth.needs_sponsorship),
        FieldKind.eeo_gender: profile.eeo.gender,
        FieldKind.eeo_race: profile.eeo.race,
        FieldKind.eeo_veteran: profile.eeo.veteran,
        FieldKind.eeo_disability: profile.eeo.disability,
    }
    value = values.get(kind)
    return value or None


def _yes_no(value: bool | None) -> str | None:
    return None if value is None else ("Yes" if value else "No")


def standard_answer(label: str, profile: Profile) -> str | None:
    """The first of your standard answers whose phrase occurs in the question."""
    haystack = question_key(label)
    for entry in profile.answers:
        needle = question_key(entry.match)
        # Anchored at a word start only, so "salary expectation" also matches
        # "salary expectations".
        if needle and re.search(rf"(?<![a-z0-9]){re.escape(needle)}", haystack):
            return entry.answer
    return None


def match_option(answer: str, options: list[dict[str, str]]) -> dict[str, str] | None:
    """Pick the option an answer refers to. Ambiguity returns ``None``, never a guess."""
    wanted = squash(answer).lower()
    if not wanted or not options:
        return None
    labels = [squash(option["label"]).lower() for option in options]

    if wanted == DECLINE:
        hits = [o for o, text in zip(options, labels, strict=True) if _DECLINE_RE.search(text)]
        return hits[0] if hits else None

    exact = [o for o, text in zip(options, labels, strict=True) if text == wanted]
    if len(exact) == 1:
        return exact[0]
    if wanted in ("yes", "no", "true", "false"):
        word = "yes" if wanted in ("yes", "true") else "no"
        hits = [
            o
            for o, text in zip(options, labels, strict=True)
            if re.match(rf"{word}\b", text)
            or text in ({"true", "y"} if word == "yes" else {"false", "n"})
        ]
        return hits[0] if len(hits) == 1 else None
    partial = [
        o
        for o, text in zip(options, labels, strict=True)
        if re.search(rf"(?<![a-z0-9]){re.escape(wanted)}(?![a-z0-9])", text)
    ]
    return partial[0] if len(partial) == 1 else None


def to_dict(form_field: FormField) -> dict[str, Any]:
    """The shape stored in ``Application.prepared`` and shown in the queue."""
    return {
        "key": form_field.key,
        "label": squash(form_field.label),
        "type": form_field.type,
        "required": form_field.required,
        "options": form_field.option_labels,
    }
