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

#: Longer descriptions are cut here before any pattern runs.
MAX_TEXT_CHARS = 120_000
_SPACES_RE = re.compile(r"[^\S\n]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_CLAUSE_SPLIT_RE = re.compile(r"[,;:()]|\band\b|\bbut\b|\bhowever\b", re.IGNORECASE)

# Matched against text whose whitespace is all single spaces, with single
# optional spaces between the parts: nothing here can be made to backtrack.
_YEARS_RE = re.compile(
    r"(?<![\d.$])(\d{1,2}) ?(?:\+|plus\b|or more\b)? ?(?:(?:-|–|to) ?\d{1,2} ?)?\+? ?"
    r"(?:years?|yrs?)\b['’]? ?(?:of )?(?:[a-z/,&\-]+ ){0,5}?experience",
    re.IGNORECASE,
)
_MIN_YEARS_RE = re.compile(
    r"(?:minimum|min\.?|at least) (?:of )?(\d{1,2})\+? ?(?:years?|yrs?)\b", re.IGNORECASE
)
# "8+ years in software engineering", "7+ YOE": the plus marks it as something asked for.
_PLUS_YEARS_RE = re.compile(
    r"(?<![\d.$\-\u2013])(?<![-\u2013] )(\d{1,2}) ?\+ ?(?:years?|yrs?|yoe)\b", re.IGNORECASE
)
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}  # fmt: skip
_NUMBER_WORD_RE = re.compile(
    r"\b(" + "|".join(_NUMBER_WORDS) + r")\b(?=\+? ?(?:\(\d{1,2}\) ?)?\+? ?(?:years?|yrs?)\b)",
    re.IGNORECASE,
)
# The years are the company's, or somebody's, not something asked of you.
_NOT_ASKED_RE = re.compile(
    r"\bcombined\b|\byears?[- ]old\b|\bin the market\b|\bin business\b|\bserving\b"
    r"|\bteam brings\b|\bleadership team\b|\bfounders?\b|\bvesting\b|\bcontract length\b"
    r"|\braised\b|\bover the (?:last|past)\b|\bdon['’]t need\b|\bdo not need\b|\bno need\b"
    r"|\bno minimum\b|\bnot required\b",
    re.IGNORECASE,
)

# ---- clearance -----------------------------------------------------------
# A sentence is about a security clearance when it names one outright, or
# says "clearance" next to something that makes it a government one. The
# word alone is not enough: "clearance of a background check" is not one.
_CLEARANCE_TERM_RE = re.compile(
    r"\bsecurity clearance\b|\bsecret(?: level)? clearance\b|\btop secret\b|\bts ?/ ?sci\b"
    r"|\bpublic trust\b|\bpolygraph\b"
    r"|\b(?:dod|government|federal|u\.?s\.? government|interim|active|current|existing)\b"
    r"[^.\n]{0,30}\bclearance\b"
    r"|\bclearance\b[^.\n]{0,30}\b(?:dod|government|federal|polygraph|sci)\b",
    re.IGNORECASE,
)
_NOT_SECURITY_RE = re.compile(
    r"\bclearance (?:of|from) (?:a |an |the )?(?:background|medical|drug|customs|credit|reference)"
    r"|\b(?:background|medical|drug|customs|credit) (?:check |screen(?:ing)? )?clearance\b"
    # Somebody else's requirement, or a process, not something asked of you.
    r"|\bclearance (?:process(?:es)?|procedures?|workflows?)\b"
    r"|\bcustomers? (?:who|that|which) require\b",
    re.IGNORECASE,
)
_OPTIONAL_RE = re.compile(
    r"\bpreferred\b|\ba plus\b|\bnice to have\b|\bdesir(?:ed|able)\b|\bencouraged\b|\bbonus\b"
    r"|\bhelpful\b|\badvantage\w*\b|\bideally\b|\boptional\b|\bwelcome\b|\bbeneficial\b",
    re.IGNORECASE,
)
_NEGATED_RE = re.compile(
    r"\bno\b|\bnot\b|n['’]t\b|\bwithout\b|\bnever\b|\bneither\b|\bnor\b|\bnone\b|\bn/a\b",
    re.IGNORECASE,
)
_OBTAIN_RE = re.compile(
    r"\b(?:ability|able|eligible|eligibility|willing(?:ness)?)\s+to\s+"
    r"(?:obtain|get|acquire|secure|receive|be granted)\b"
    r"|\b(?:obtain|acquire)(?:ing)?\b[^.\n]{0,30}\bclearance\b",
    re.IGNORECASE,
)
_REQUIRED_RE = re.compile(
    r"\brequired?\b|\brequires\b|\bmust\b|\bmandatory\b|\bnecessary\b|\bessential\b|\bneed(?:ed|s)?\b",
    re.IGNORECASE,
)
# A list item that is nothing but the clearance: "Active TS/SCI clearance".
_BARE_CLEARANCE_RE = re.compile(
    r"[-*•\s]*(?:an? )?(?:active|current|existing)\b.{0,40}\bclearance\b.{0,25}", re.IGNORECASE
)
_CLEARANCE_LEVELS = [
    ("TS/SCI", re.compile(r"\bts ?/ ?sci\b", re.IGNORECASE)),
    ("Top Secret", re.compile(r"\btop secret\b", re.IGNORECASE)),
    (
        "Secret",
        re.compile(r"\bsecret clearance\b|\bsecret\b(?=[^.\n]{0,20}clearance)", re.IGNORECASE),
    ),
    ("Public Trust", re.compile(r"\bpublic trust\b", re.IGNORECASE)),
]

# ---- sponsorship ---------------------------------------------------------
_SPONSOR_WORD_RE = re.compile(r"\bsponsor(?:ship|ing|s|ed)?\b", re.IGNORECASE)
_SPONSOR_NEGATED_RE = re.compile(
    r"\b(?:unable|unavailable|cannot|can not|no|not|never|without)\b|n['’]t\b",
    re.IGNORECASE,
)
_SPONSOR_OFFERED_RE = re.compile(
    r"\bsponsorship\b[^.\n]{0,25}\b(?:available|offered|provided|possible)\b"
    r"|\b(?:we|will|can|do|does|may)\s+(?:also\s+)?sponsor\b"
    r"|\b(?:happy|able|willing|glad|open)\s+to\s+sponsor\b"
    r"|\b(?:offers?|provides?)\s+(?:visa\s+|h-?1b\s+)?sponsorship\b",
    re.IGNORECASE,
)
# What makes a sentence about sponsoring a person to work, and what makes it
# about sponsoring something else (a conference, a community, a colleague).
_VISA_CUE_RE = re.compile(
    r"\bvisas?\b|\bh-?1b\b|\bimmigration\b|\bwork (?:authori[sz]ation|permit)s?\b"
    r"|\bemployment (?:visa|authori[sz]ation)\b|\bgreen card\b",
    re.IGNORECASE,
)
_OTHER_SPONSOR_RE = re.compile(
    r"\b(?:event|conference|corporate|community|executive|brand|sports?) sponsor\w*"
    r"|\bsponsorships? (?:programs?|budgets?|deals?|packages?|opportunit\w+|revenue|sales)\b"
    r"|\bsponsor[- ](?:led|banks?)\b"
    r"|\bsponsor(?:s|ing|ed)? (?:\w+ ){0,3}?(?:conferences?|events?|certifications?|communit\w+"
    r"|meetups?|attendance|engineers?|training|hackathons?|open[- ]source|programs?)\b",
    re.IGNORECASE,
)
# "Visa sponsorship: No", "Sponsorship available: yes".
_SPONSOR_LABEL_RE = re.compile(
    r"\bsponsorship(?: available| offered| provided)?\s*:\s*"
    r"(yes|available|no|none|not available|unavailable|n/a)\b",
    re.IGNORECASE,
)

# ---- travel --------------------------------------------------------------
_TRAVEL_KIND = (
    r"(?:(?:domestic|international|overnight|business|regional|local|global|required|expected|"
    r"occasional|client|customer|work[- ]related) )"
)
_PERCENT = r"(?: ?%| percent\b)"
_TRAVEL_BEFORE_RE = re.compile(
    rf"(?<![\d.])(\d{{1,3}}){_PERCENT} ?(?:of (?:the )?time )?(?:(?:of|for|in) )?{_TRAVEL_KIND}*travel\b"
    r"(?! (?:costs?|expenses?|reimburs\w*|insurance|stipend|budget|allowance|booking|benefits?"
    r"|industry|tech|time|emissions?|polic\w+|platform|spend))",
    re.IGNORECASE,
)
_TRAVEL_AFTER_RE = re.compile(
    r"\btravel(?:l?ing)?\b(?: requirements?| required| percentage| expectations?)?:?"
    rf"([^.,;%\n]{{0,40}}?)(?<![\d.])(\d{{1,3}}){_PERCENT}"
    # "travel 100% remote", "50% off flights": the figure is about something else.
    r"(?! ?(?:remote|paid|covered|employer|company|off\b|match|discount"
    r"|of (?:your |the )?(?:premiums?|costs?|expenses?|flights?|travel)))",
    re.IGNORECASE,
)
# Words that make the number after "travel" something other than travel time.
_NOT_TRAVEL_RE = re.compile(
    r"remote|paid|match|premium|uptime|cover|reimburs|cost|expens|insur|401|bonus|discount|"
    r"salary|equity|booking|api|industry|budget|stipend|benefit|perk|emission|polic|platform|"
    r"spend|\btime by\b|\bby\b",
    re.IGNORECASE,
)
_ONCALL_RE = re.compile(r"\bon[- ]call\b", re.IGNORECASE)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)
# In running text "hybrid" is usually about clouds. Only wording about where
# the work happens counts.
_HYBRID_WORK_RE = re.compile(
    r"\bhybrid (?:role|position|work(?:ing)?|schedule|arrangement|policy|remote|in[- ]office|office)\b"
    r"|\b(?:this|the) (?:role|position|job) is hybrid\b"
    r"|\bhybrid\b[^.\n]{0,30}\b(?:days? (?:a|per|each) week|in[- ]office|on[- ]?site|in[- ]person)\b"
    r"|\b(?:\d|one|two|three|four) days? (?:a|per|each) week (?:in|at|from) (?:the |our )?office\b",
    re.IGNORECASE,
)
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
    """The years of experience the posting asks for, when it asks.

    Read one sentence at a time, so that a figure about the company ("20
    years in the market"), a figure it says you do not need, and one that is
    only a nice-to-have are left out.
    """
    # List items and paragraphs are sentences of their own; a line break
    # inside one (the hard wrap of an email) is just a space.
    marked = re.sub(r"\n\s*(?:[-*\u2022]\s+|\n)", ". ", text)
    flat = re.sub(r"\bmin\.", "minimum", " ".join(marked.split()), flags=re.IGNORECASE)
    flat = _NUMBER_WORD_RE.sub(lambda m: str(_NUMBER_WORDS[m.group(1).lower()]), flat)
    found: list[int] = []
    for sentence in re.split(r"(?<=[.!?])\s+", flat):
        if _NOT_ASKED_RE.search(sentence):
            continue
        for pattern in (_YEARS_RE, _MIN_YEARS_RE, _PLUS_YEARS_RE):
            for match in pattern.finditer(sentence):
                if not _OPTIONAL_RE.search(_clause_around(sentence, match.start(), match.end())):
                    found.append(int(match.group(1)))
    plausible = [years for years in found if 1 <= years <= 30]
    # The most demanding figure is the role's real bar ("12+ years, 5+ leading teams").
    return max(plausible) if plausible else None


def _clause_around(sentence: str, start: int, end: int, reach: int = 200) -> str:
    """The stretch between commas that holds ``sentence[start:end]``.

    "Nice to have: 20+ years of COBOL" and "2+ years of Rust preferred" are
    one clause each; ", cloud certification preferred" after a figure is the
    next one. Looks no further than ``reach`` characters either way.
    """
    before = sentence[max(0, start - reach) : start]
    after = sentence[end : end + reach]
    left = max(before.rfind(","), before.rfind(";")) + 1
    cuts = [index for index in (after.find(","), after.find(";")) if index >= 0]
    return before[left:] + sentence[start:end] + after[: min(cuts) if cuts else len(after)]


def _sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in _SENTENCE_SPLIT_RE.split(text) if sentence.strip()]


def _clearance(text: str) -> tuple[str | None, str | None]:
    """``("required" | "obtainable" | None, level)``.

    Judged one sentence at a time. "Required" needs a sentence that is about a
    security clearance and says it is needed, with nothing in it that turns
    that around: no negation ("no clearance required"), nothing optional ("a
    plus", "preferred", "encouraged to apply") and no "able to obtain".
    """
    required = obtainable = False
    for sentence in _sentences(text):
        if not _CLEARANCE_TERM_RE.search(sentence) or _NOT_SECURITY_RE.search(sentence):
            continue
        if _OBTAIN_RE.search(sentence):
            obtainable = True
            continue
        if _OPTIONAL_RE.search(sentence) or _NEGATED_RE.search(sentence):
            continue
        short = len(sentence.split()) <= 8 and _BARE_CLEARANCE_RE.fullmatch(sentence)
        if _REQUIRED_RE.search(sentence) or short:
            required = True
    if not required and not obtainable:
        return None, None
    level = next((name for name, pattern in _CLEARANCE_LEVELS if pattern.search(text)), None)
    return ("required" if required else "obtainable"), level


def _sponsorship(text: str) -> str | None:
    """``"not_offered"``, ``"offered"`` or ``None``, judged clause by clause.

    A "not" only counts in the clause that mentions sponsorship ("not an
    entry-level role and sponsorship is available" offers it), and "with or
    without sponsorship" says nothing either way.
    """
    offered = False
    for sentence in _sentences(text):
        if not _SPONSOR_WORD_RE.search(sentence):
            continue
        if not _VISA_CUE_RE.search(sentence) and _OTHER_SPONSOR_RE.search(sentence):
            continue  # "we sponsor conferences": not about your right to work
        if re.search(r"\bwith or without\b", sentence, re.IGNORECASE):
            continue
        label = _SPONSOR_LABEL_RE.search(sentence)
        if label:
            if label.group(1).lower() not in ("yes", "available"):
                return "not_offered"
            offered = True
            continue
        for clause in _CLAUSE_SPLIT_RE.split(sentence):
            if not _SPONSOR_WORD_RE.search(clause):
                continue
            if _SPONSOR_NEGATED_RE.search(clause):
                return "not_offered"
            if _SPONSOR_OFFERED_RE.search(clause):
                offered = True
    return "offered" if offered else None


def _travel(text: str) -> int | None:
    """The share of time spent travelling, when the posting states one.

    The number has to belong to "travel" itself: right before it ("25%
    travel", "up to 20% international travel") or right after it on the same
    line ("Travel: 10%"). "100% remote", benefit percentages and uptime
    figures that merely sit near the word are not travel.
    """
    values = [int(match.group(1)) for match in _TRAVEL_BEFORE_RE.finditer(text)]
    for match in _TRAVEL_AFTER_RE.finditer(text):
        if not _NOT_TRAVEL_RE.search(match.group(1)):
            values.append(int(match.group(2)))
    values = [value for value in values if value <= 100]
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
    if _HYBRID_WORK_RE.search(description):
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
    # All runs of spaces, tabs, form feeds and the like become one space
    # (line breaks are kept): the patterns below then run in linear time.
    text = _SPACES_RE.sub(" ", (description or "")[:MAX_TEXT_CHARS])
    facts: dict[str, Any] = {}

    years = _years(text)
    if years is not None:
        facts["years_required"] = years

    clearance, level = _clearance(text)
    if clearance:
        facts["clearance"] = clearance
        if level:
            facts["clearance_level"] = level

    sponsorship = _sponsorship(text)
    if sponsorship:
        facts["sponsorship"] = sponsorship

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
