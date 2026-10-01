"""Job sources: one adapter per applicant-tracking system."""

from __future__ import annotations

import re

from jobportal.http import PoliteClient
from jobportal.sources.ashby import AshbyAdapter
from jobportal.sources.base import (
    CrawlContext,
    Listing,
    RawJob,
    SourceAdapter,
    SourceRef,
    SourceSpec,
)
from jobportal.sources.greenhouse import GreenhouseAdapter
from jobportal.sources.lever import LeverAdapter
from jobportal.sources.workday import WorkdayAdapter

ADAPTERS: dict[str, SourceAdapter] = {
    adapter.kind: adapter
    for adapter in (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), WorkdayAdapter())
}

__all__ = [
    "ADAPTERS",
    "CrawlContext",
    "Listing",
    "RawJob",
    "SourceAdapter",
    "SourceRef",
    "SourceSpec",
    "detect_source",
    "discover_sources",
    "get_adapter",
]

# Board links as they appear inside a company's own careers page.
_LINK_RE = re.compile(
    r"""https?://(?:
        (?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/[^\s"'<>)]+ |
        boards-api\.greenhouse\.io/v1/boards/[^\s"'<>)]+ |
        jobs(?:\.eu)?\.lever\.co/[^\s"'<>)]+ |
        jobs\.ashbyhq\.com/[^\s"'<>)]+ |
        [a-z0-9_-]+\.wd\d+\.myworkdayjobs\.com/[^\s"'<>)]+ |
        wd\d+\.myworkdaysite\.com/recruiting/[^\s"'<>)]+
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def get_adapter(kind: str) -> SourceAdapter | None:
    return ADAPTERS.get(kind)


def detect_source(url: str) -> SourceSpec | None:
    """Recognise a board URL of any supported ATS."""
    for adapter in ADAPTERS.values():
        spec = adapter.detect(url.strip())
        if spec is not None:
            return spec
    return None


def discover_sources(client: PoliteClient, careers_url: str) -> list[SourceSpec]:
    """Find the ATS board(s) a company's careers page links to or embeds."""
    direct = detect_source(careers_url)
    if direct is not None:
        return [direct]
    response = client.get(careers_url, headers={"Accept": "text/html"})
    # Links inside inline JSON are written with escaped slashes.
    page = response.text.replace("\\/", "/")
    specs: dict[tuple[str, str], SourceSpec] = {}
    for candidate in [response.url, *_LINK_RE.findall(page)]:
        spec = detect_source(candidate)
        if spec is not None:
            specs.setdefault((spec.kind, spec.token), spec)
    return list(specs.values())
