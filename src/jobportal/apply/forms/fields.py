"""Understand application-form fields and decide what goes in each.

The rule throughout: an answer is either something you configured, something
you answered before, or it is missing. Nothing is guessed.

A field is recognised as one of yours (name, email, total years of
experience, a self-identification question) only when its whole label says
so, not when it merely contains the word. Questions with legal weight (work
authorisation, sponsorship) are answered from your profile only when the
whole question is one of a few standard phrasings, names your country and no
other, and offers a plain Yes and No. Anything else is handed to you once,
and your answer is remembered for that exact question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any

from jobportal.config import Profile
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
# An option that declines to self-identify: "Decline to self identify",
# "I prefer not to say", a bare "I decline". Not any option that happens to
# contain the word ("I decline the background check").
_DECLINE_VERB = (
    r"(?:decline|prefer not|choose not|(?:would )?rather not|do(?:n't| not) (?:wish|want)|not wish)"
)
_DECLINE_WHAT = (
    r"(?: to)? (?:self[- ]?identify|identify|answer|say|disclose|specify|state|respond|share|"
    r"provide|self[- ]?disclose)"
)
_DECLINE_RE = re.compile(
    rf"^(?:i )?{_DECLINE_VERB}(?:{_DECLINE_WHAT}\b.*)?$|\b{_DECLINE_VERB}{_DECLINE_WHAT}\b",
    re.IGNORECASE,
)
LEGAL_KINDS = frozenset({FieldKind.work_authorized, FieldKind.needs_sponsorship})


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
    #: How the label is tied to the control: ``for``, ``wrap``, ``aria-label``,
    #: ``aria-labelledby``, ``legend``, ``group``, ``nearby`` (unbound text in
    #: the field's own box), ``placeholder``, or empty when there is none.
    label_source: str = ""
    #: What the site pre-filled, as shown to a person.
    current: str = ""

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

# Each pattern must match the *whole* label (as bare lower-case words), so
# that "City of birth", "Phone extension" or "Which city are you applying
# for?" are not taken for your city or phone. A label these do not cover is
# simply asked once and remembered.
_LABEL_RULES: list[tuple[FieldKind, re.Pattern[str]]] = [
    (FieldKind.linkedin, re.compile(r"(?:your )?linked ?in(?: profile)?(?: url| link)?")),
    (FieldKind.github, re.compile(r"(?:your )?git ?hub(?: profile)?(?: url| link)?")),
    (
        FieldKind.website,
        re.compile(
            r"(?:your )?(?:personal )?(?:website|portfolio|blog|personal site)(?: url| link)?"
            r"|website or portfolio|portfolio or website|portfolio website"
        ),
    ),
    (
        FieldKind.preferred_name,
        re.compile(r"(?:preferred|nick) ?(?:first )?name|nickname|what name do you go by"),
    ),
    (FieldKind.first_name, re.compile(r"(?:your )?(?:legal )?(?:first|given) ?name")),
    (FieldKind.last_name, re.compile(r"(?:your )?(?:legal )?(?:last|family) ?name|surname")),
    (
        FieldKind.current_company,
        re.compile(
            r"(?:current|present|most recent) (?:company|employer)(?: name)?"
            r"|(?:company|employer|organization|organisation|org)(?: name)?"
        ),
    ),
    (
        FieldKind.current_title,
        re.compile(
            r"(?:current|present|most recent) (?:job )?(?:title|role|position)|(?:job )?title"
        ),
    ),
    (
        FieldKind.full_name,
        re.compile(r"(?:your )?(?:full legal |full |legal )?name(?: first and last)?"),
    ),
    (FieldKind.email, re.compile(r"(?:your )?e ?mail(?: address)?")),
    (
        FieldKind.phone,
        re.compile(
            r"(?:your )?(?:phone|mobile|cell|telephone|mobile phone|cell phone|contact number)"
            r"(?: number| no)?"
        ),
    ),
    (
        FieldKind.location,
        re.compile(
            r"(?:your )?(?:current )?location(?: city)?(?: state)?(?: country)?"
            r"|where are you (?:currently )?(?:located|based)|city (?:and )?state(?: country)?"
        ),
    ),
    (FieldKind.postal_code, re.compile(r"(?:zip|postal)(?: code)?|zip postal code|postcode")),
    (FieldKind.city, re.compile(r"(?:current )?city|city of residence|city town")),
    (
        FieldKind.region,
        re.compile(r"state|province|region|state province|state or province|state province region"),
    ),
    (FieldKind.country, re.compile(r"(?:current )?country|country of residence")),
]
# A resume or cover letter is only ever an upload. A text box that mentions
# one ("Link to resume", "Paste your cover letter") is a question for you.
_DOCUMENT_RE = re.compile(r"\b(resume|résumé|cv|curriculum vitae|cover letter)\b", re.I)
# Total experience, and nothing narrower: "years of experience with Rust" or
# "relevant experience" is not something your total answers.
_KINDS_OF_EXPERIENCE = r"(?:(?:professional|work|working|total|overall|full time) )*"
_YEARS_RE = re.compile(
    rf"(?:total |overall )?(?:number of )?years(?: of)? {_KINDS_OF_EXPERIENCE}experience(?: in total)?"
    rf"|how many years of {_KINDS_OF_EXPERIENCE}experience do you have(?: in total| overall| altogether)?"
)
# Labels about another person. Their name, email or phone is never yours.
_SOMEONE_ELSE_RE = re.compile(
    r"\b(referr(?:er|al|ed|ing)|referenc\w*|referee|emergency|next of kin|manager|supervisor|"
    r"recruiter|spouse|partner|parent|guardian|friend|colleague|co-?worker|contact person|"
    r"employee who|who (?:referred|recommended)|someone (?:we|you))\b"
    r"|\b(?!(?:your|what|it|that|here|there|who|let)['’]s\b)[a-z]+['’]s\b",
    re.I,
)
# Self-identification questions, matched against the whole label.
_EEO_RULES: list[tuple[FieldKind, re.Pattern[str]]] = [
    (
        FieldKind.eeo_gender,
        re.compile(
            r"(?:what is your |please (?:select|indicate|identify) your |i identify my )?"
            r"(?:gender|sex)(?: identity)?(?: as)?|(?:what |which )?gender do you identify (?:as|with)"
        ),
    ),
    (
        FieldKind.eeo_race,
        re.compile(
            r"(?:what is your |please (?:select|indicate|identify) your )?"
            r"(?:race|ethnicity|race ethnicity|race and ethnicity|race or ethnicity|ethnic (?:background|group|origin)|"
            r"racial ethnic (?:background|identity|group)|hispanic latino|hispanic or latino)"
            r"|are you hispanic(?: or)? latin[oax]"
        ),
    ),
    (
        FieldKind.eeo_veteran,
        re.compile(
            r"(?:what is your |please (?:select|indicate|identify) your )?(?:protected )?veteran status"
            r"|(?:are you|do you identify as) a (?:protected )?veteran"
        ),
    ),
    (
        FieldKind.eeo_disability,
        re.compile(
            r"(?:what is your |please (?:select|indicate|identify) your )?disability(?: status)?"
            r"|do you (?:have|identify as having) a disability(?: or have you ever had (?:one|a disability))?"
            r"|voluntary self identification of disability"
        ),
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
# Any of these words makes a question a legal one. If it is not then one of
# the exact phrasings below, it is never answered automatically.
_LEGAL_WORDS_RE = re.compile(
    r"authori[sz]|sponsor|\bvisas?\b|citizen|\bwork permit|right to work|"
    r"\b(?:eligible|permitted|allowed|entitled) to work\b|clearance|immigration|green card|\bh-?1b\b",
    re.IGNORECASE,
)
_US_NAMES = {"united states", "united states of america", "us", "usa", "u.s.", "u.s.a."}
# An example in brackets ("(e.g., H-1B visa status)") is not part of the question.
_EXAMPLE_RE = re.compile(
    r"[(\[]\s*(?:e\.?\s?g\.?|i\.?\s?e\.?|for example|such as)\b[^)\]]*[)\]]", re.I
)


def plain(label: str) -> str:
    """The label as bare lower-case words, for matching whole questions."""
    text = _EXAMPLE_RE.sub(" ", label).lower().replace("\u2019", "'")
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text).split())


@lru_cache(maxsize=16)
def _legal_patterns(country: str) -> tuple[re.Pattern[str], re.Pattern[str]] | None:
    """Whole-question patterns for the two legal questions, for one country."""
    name = country.strip().lower()
    if not name:
        return None
    if name in _US_NAMES:
        place = r"(?:the )?(?:united states(?: of america)?|u s a|u s|usa|us)"
    else:
        place = r"(?:the )?" + re.escape(plain(name))
    authorised = re.compile(
        rf"(?:are|will) you (?:be )?(?:(?:currently|legally|lawfully) )*"
        rf"(?:authori[sz]ed|eligible|permitted|allowed|entitled) to work (?:(?:legally|lawfully) )?in {place}"
        rf"(?: for any employer)?"
        rf"|do you (?:currently )?have (?:the )?(?:legal |lawful )?(?:right|authori[sz]ation|permission) "
        rf"to work in {place}"
        rf"|(?:(?:legally|lawfully) )?(?:authori[sz]ed|eligible) to work in {place}"
    )
    when = r"(?: now or in (?:the )?future| in (?:the )?future| currently| now)?"
    sponsorship = re.compile(
        rf"(?:(?:will|do|would) you|do you now or will you in the future){when} (?:require|need){when} "
        rf"(?:(?:visa|employment|immigration|work|employer|company) )*sponsorship"
        rf"(?: for (?:an? )?(?:employment|work|immigration) (?:visa|authori[sz]ation|permit)(?: status)?)?"
        rf"{when}(?: (?:in order )?to (?:work|be employed)(?: legally| lawfully)? in {place}| in {place}){when}"
    )
    return authorised, sponsorship


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
        words = plain(label)
        for kind, pattern in _EEO_RULES:
            if pattern.fullmatch(words):
                return kind

    legal = _legal_kind(label, profile)
    if legal is not None:
        return legal
    if _LEGAL_WORDS_RE.search(label):
        return FieldKind.question  # legal wording we do not recognise exactly: ask you
    if _SOMEONE_ELSE_RE.search(label) or _DOCUMENT_RE.search(label):
        return FieldKind.question
    if _YEARS_RE.fullmatch(plain(label)):
        return FieldKind.years_experience

    words = plain(label)
    for kind, pattern in _LABEL_RULES:
        if pattern.fullmatch(words):
            return kind
    if not label:
        # Nothing to read: the control's own type says what it wants.
        if form_field.type == "email":
            return FieldKind.email
        if form_field.type == "tel":
            return FieldKind.phone
    if len(label) <= 60:
        # The board's own field name ("name", "email", "org" on Lever).
        hinted = _NAME_HINTS.get(lowered_name)
        if hinted is not None and hinted is not FieldKind.resume:
            return hinted
    return FieldKind.question


def _legal_kind(label: str, profile: Profile) -> FieldKind | None:
    """Recognise only the plain, standard wording of the two legal questions."""
    patterns = _legal_patterns(profile.work_authorization.country)
    if patterns is None or len(label) > 220:
        return None
    authorised, sponsorship = patterns
    words = plain(label)
    if authorised.fullmatch(words):
        return FieldKind.work_authorized
    if sponsorship.fullmatch(words):
        return FieldKind.needs_sponsorship
    return None


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


# A question that joins or qualifies things ("located in or willing to relocate
# to Austin?") is not answered by a phrase that matches half of it.
_COMPOUND_RE = re.compile(
    r"\b(?:or|nor|not|never|unless|except|if|either|neither|without|other than|but)\b|n['\u2019]t\b",
    re.IGNORECASE,
)


def standard_answer(label: str, profile: Profile) -> str | None:
    """The first of your standard answers whose phrase occurs in a simple question."""
    if _LEGAL_WORDS_RE.search(label) or _COMPOUND_RE.search(label):
        return None
    haystack = question_key(label)
    for entry in profile.answers:
        needle = question_key(entry.match)
        # Anchored at a word start only, so "salary expectation" also matches
        # "salary expectations".
        if needle and re.search(rf"(?<![a-z0-9]){re.escape(needle)}", haystack):
            return entry.answer
    return None


_QUALIFIER_RE = re.compile(
    r"\b(?:no|not|non|never|none|without|except|unless|but|under|over|less|more|than|only|"
    r"former|ex|previous|other)\b|n['\u2019]t\b",
    re.IGNORECASE,
)


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
        # Only a bare Yes or No. "No, I am a citizen" or "Yes, but I will need
        # sponsorship" says more than your answer does, so it is never picked.
        accepted = {"yes", "true", "y"} if wanted in ("yes", "true") else {"no", "false", "n"}
        hits = [
            o for o, text in zip(options, labels, strict=True) if text.rstrip(" .!") in accepted
        ]
        return hits[0] if len(hits) == 1 else None
    if not re.search(r"[a-z]{3}", wanted):
        return None  # a number or a code picks an option only when it is the whole option
    # Otherwise the answer may be most of an option ("Referral" for "Employee
    # referral"), as long as what the option adds is short and does not turn
    # the meaning around.
    partial = []
    for option, text in zip(options, labels, strict=True):
        found = re.search(rf"(?<![a-z0-9]){re.escape(wanted)}(?![a-z0-9])", text)
        if not found:
            continue
        rest = f"{text[: found.start()]} {text[found.end() :]}"
        if len(rest.split()) <= 3 and not _QUALIFIER_RE.search(rest):
            partial.append(option)
    return partial[0] if len(partial) == 1 else None


def to_dict(form_field: FormField) -> dict[str, Any]:
    """The shape stored in ``Application.prepared`` and shown in the queue."""
    return {
        "key": form_field.key,
        "label": squash(form_field.label),
        "type": form_field.type,
        "required": form_field.required,
        "options": form_field.option_labels,
        "current": form_field.current,
    }
