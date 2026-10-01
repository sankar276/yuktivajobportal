"""Common types for job sources."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from jobportal.config import Employment
from jobportal.http import PoliteClient


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


def infer_remote(location: str | None, workplace_type: str | None = None) -> bool | None:
    """True/False when the posting says so, ``None`` when it does not say."""
    workplace = (workplace_type or "").strip().lower().replace("_", "-")
    if workplace in ("remote", "fully-remote"):
        return True
    if workplace in ("on-site", "onsite", "hybrid", "in-office"):
        return False
    text = (location or "").lower()
    if not text:
        return None
    if "remote" in text or "work from home" in text or "anywhere" in text:
        return True
    if "hybrid" in text or "on-site" in text or "onsite" in text:
        return False
    return None


_REMOTE_TEXT_RE = re.compile(
    r"\b(?:fully|100%|completely|entirely)[ -]remote\b"
    r"|\bremote[- ]first\b"
    r"|\bthis (?:is a|role is|position is|job is)(?: a)? (?:fully |100% )?remote\b"
    r"|\bwork(?:ing)? from anywhere\b",
    re.IGNORECASE,
)


def remote_from_description(text: str | None) -> bool | None:
    """True when the description itself says the role is remote; otherwise unknown."""
    return True if text and _REMOTE_TEXT_RE.search(text) else None


def clean_token(value: str) -> str:
    return value.strip().strip("/")
