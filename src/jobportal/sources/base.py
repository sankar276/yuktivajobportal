"""Common types for job sources."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

from jobportal.config import Employment
from jobportal.http import FetchError, PoliteClient

log = logging.getLogger(__name__)


@dataclass
class RawJob:
    """A posting as a source reports it, before normalisation."""

    external_id: str
    title: str
    company: str = ""
    url: str = ""
    apply_url: str | None = None
    location: str = ""
    remote: bool | None = None
    employment_type: str | None = None
    department: str = ""
    requisition_id: str = ""
    description_html: str = ""
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    comp_min: float | None = None
    comp_max: float | None = None
    comp_currency: str | None = None
    comp_period: str | None = None
    #: The listing had no description; ``fetch_detail`` fills it in.
    needs_detail: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Listing:
    jobs: list[RawJob] = field(default_factory=list)
    #: True when ``jobs`` is every open posting of the source, which lets the
    #: crawler mark postings that disappeared as closed.
    complete: bool = True
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class SourceRef:
    """An immutable snapshot of a ``Source`` row, safe to hand to worker threads."""

    id: int
    kind: str
    token: str
    company_name: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None

    @property
    def label(self) -> str:
        return self.company_name or self.token


@dataclass(frozen=True)
class SourceSpec:
    """What is needed to create a source, as recognised from a URL."""

    kind: str
    token: str
    company_name: str = ""
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrawlContext:
    #: Search terms for sources that are queried rather than listed in full.
    search_terms: tuple[str, ...] = ()


class SourceAdapter(ABC):
    kind: ClassVar[str]
    #: Shown in the UI.
    display_name: ClassVar[str]

    @abstractmethod
    def list_jobs(self, client: PoliteClient, source: SourceRef, context: CrawlContext) -> Listing:
        """Fetch the current postings of ``source``."""

    def fetch_detail(self, client: PoliteClient, source: SourceRef, job: RawJob) -> RawJob:
        """Fill in what the listing left out. Only called when ``needs_detail``."""
        return job

    @classmethod
    @abstractmethod
    def detect(cls, url: str) -> SourceSpec | None:
        """Recognise one of this ATS's board URLs."""

    @abstractmethod
    def board_url(self, source: SourceRef) -> str:
        """The human-facing careers page for this source."""


# ----------------------------------------------------------- shared helpers


def parse_datetime(value: Any) -> datetime | None:
    """ISO-8601 strings and epoch milliseconds to aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


_EMPLOYMENT_PATTERNS: list[tuple[re.Pattern[str], Employment]] = [
    (re.compile(r"\bintern(ship)?\b", re.I), Employment.internship),
    (
        re.compile(r"contract|freelance|\bc2c\b|corp[- ]to[- ]corp|\b1099\b", re.I),
        Employment.contract,
    ),
    (re.compile(r"part[\s_-]*time", re.I), Employment.part_time),
    (re.compile(r"\btemp(orary)?\b", re.I), Employment.temporary),
    (re.compile(r"full[\s_-]*time|permanent|regular|\bfte\b", re.I), Employment.full_time),
]


def parse_employment(value: str | None) -> str | None:
    """Map an ATS's free-form commitment label to an :class:`Employment` value."""
    if not value:
        return None
    for pattern, employment in _EMPLOYMENT_PATTERNS:
        if pattern.search(value):
            return employment.value
    return None


REMOTE_WORDS_RE = re.compile(
    r"\bremote\b(?!\s+sensing)|\bvirtual\b|\btelecommut\w*|\bwork(?:ing)? from home\b|\bwfh\b"
    r"|\bhome[- ]?based\b|\bhome office\b|\banywhere\b|\bnationwide\b",
    re.IGNORECASE,
)
_PLACE_BOUND_RE = re.compile(
    r"\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b|\bhybrid\b|\boffice[- ]based\b",
    re.IGNORECASE,
)
_NOT_REMOTE_RE = re.compile(r"\b(?:not|non|no)[\s-]+(?:fully[\s-]+)?remote\b", re.IGNORECASE)
#: No location is longer than this; what a board sends beyond it is not read.
MAX_LOCATION_CHARS = 500
_SEGMENT_RE = re.compile(r";|\|| or | / ")


def segments(location: str | None) -> list[str]:
    """The places a location lists ("Austin, TX; Remote"), each to be read on its own.

    Blanks are collapsed first, so that nothing a board sends (a hundred
    thousand spaces, say) can make the splitting slow.
    """
    text = " ".join((location or "")[:MAX_LOCATION_CHARS].replace("\n", ";").split())
    return [part.strip() for part in _SEGMENT_RE.split(text) if part.strip()]


def _reading(part: str) -> str:
    """What one place says about where the work is done: remote, bound, mixed or nothing."""
    if _NOT_REMOTE_RE.search(part):
        return "bound"  # "Not Remote - Seattle", "Onsite (no remote)"
    remote = bool(REMOTE_WORDS_RE.search(part))
    bound = bool(_PLACE_BOUND_RE.search(part))
    if remote and bound:
        return "mixed"  # "Hybrid (2 days remote)", "Hybrid/Remote - NYC"
    return "remote" if remote else ("bound" if bound else "")


def _readings(location: str) -> list[str]:
    return [_reading(part) for part in segments(location)]


def remote_unclear(location: str | None) -> bool:
    """The location says both "remote" and "hybrid" or "on-site" of the same place."""
    readings = _readings(location or "")
    return "remote" not in readings and "mixed" in readings


def infer_remote(location: str | None, workplace_type: str | None = None) -> bool | None:
    """True/False when the posting says so, ``None`` when it does not say.

    A declared workplace type decides, whatever its spelling ("Remote",
    "Fully Remote", "Remote Eligible", "Virtual", "On Site", "HYBRID").
    Otherwise the location string is read, one listed place at a time: any
    place that is plainly remote makes the role remote; a place that says
    both ("Hybrid (2 days remote)") is left as not known rather than guessed.
    """
    workplace = re.sub(r"[\s_-]+", " ", (workplace_type or "").strip())
    if workplace:
        if _PLACE_BOUND_RE.search(workplace):
            return False
        if REMOTE_WORDS_RE.search(workplace):
            return True
    readings = _readings(location or "")
    if "remote" in readings:
        return True
    if "mixed" in readings:
        return None
    return False if "bound" in readings else None


_REMOTE_TEXT_RE = re.compile(
    r"\b(?:fully|100%|completely|entirely)[ -]remote\b"
    r"|\bremote[- ]first\b"
    r"|\bthis (?:is a|role is|position is|job is)(?: a)? (?:fully |100% )?remote\b"
    r"|\bwork(?:ing)? from anywhere\b",
    re.IGNORECASE,
)
_NOT_BEFORE_RE = re.compile(r"\b(?:not|no|non|never)\b|n['’]t\b", re.IGNORECASE)
# The same description also ties this role to an office: "a remote-first
# company, but this role is based in our NYC office five days a week".
_OFFICE_BOUND_RE = re.compile(
    r"\b(?:this|the) (?:role|position|job)\b[^.\n]{0,40}"
    r"\b(?:based|located|on[- ]?site|in[- ]office|in[- ]person|hybrid)\b"
    r"|\b(?:\d|one|two|three|four|five) days? (?:a|per|each) week\b[^.\n]{0,25}\b(?:office|on[- ]?site)"
    r"|\b(?:office|on[- ]?site)\b[^.\n]{0,25}\b(?:\d|one|two|three|four|five) days? (?:a|per|each) week\b",
    re.IGNORECASE,
)


def remote_from_description(text: str | None) -> bool | None:
    """True when the description itself says the role is remote; otherwise unknown.

    "This role is not fully remote" and "this is not a 100% remote position"
    say the opposite, so a match with a negation just before it does not count.
    """
    for match in _REMOTE_TEXT_RE.finditer(text or ""):
        before = (text or "")[max(0, match.start() - 40) : match.start()]
        clause = re.split(r"[.;\n]", before)[-1]
        if not _NOT_BEFORE_RE.search(clause) and not _NOT_BEFORE_RE.search(match.group(0)):
            # Said to be remote, and also tied to an office: not for us to settle.
            return None if _OFFICE_BOUND_RE.search(text or "") else True
    return None


def web_url(value: Any) -> str:
    """``value`` when it is a plain http(s) address, otherwise an empty string.

    Posting and application links come from the boards. Anything else (a
    ``javascript:`` link, a ``data:`` blob) must never be stored, shown as a
    link or handed to the browser.
    """
    text = str(value or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    return text if parts.scheme in ("http", "https") and parts.hostname else ""


def listed(payload: Any, key: str | None, where: str) -> list[dict[str, Any]]:
    """The postings in a board's answer; a :class:`FetchError` when it is not shaped as expected.

    An answer without its list (``{"jobs": null}``, an error object) is a
    failed fetch. Treating it as "no postings" would close every job.
    """
    items = payload if key is None else (payload.get(key) if isinstance(payload, dict) else None)
    if not isinstance(items, list):
        raise FetchError(f"{where}: the board's answer did not contain a list of postings")
    return [item for item in items if isinstance(item, dict)]


def build_jobs(
    items: list[dict[str, Any]], build: Callable[[dict[str, Any]], RawJob | None], where: str
) -> list[RawJob]:
    """Turn postings into jobs one at a time, so one malformed posting costs only itself."""
    jobs: list[RawJob] = []
    failed = 0
    for item in items:
        try:
            job = build(item)
        except Exception as exc:
            failed += 1
            log.warning(
                "%s: skipped a posting that could not be read (%s: %s)",
                where,
                type(exc).__name__,
                exc,
            )
            continue
        if job is not None:
            jobs.append(job)
    if failed and not jobs:
        raise FetchError(f"{where}: none of the {failed} postings could be read")
    return jobs


def text_of(value: Any) -> str:
    """A name out of something that is either the name or an object carrying it."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("descriptor", "name", "location", "label", "text"):
            if isinstance(value.get(key), str):
                return str(value[key]).strip()
    return ""


def clean_token(value: str) -> str:
    return value.strip().strip("/")
