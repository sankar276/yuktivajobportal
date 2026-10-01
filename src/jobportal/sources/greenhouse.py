"""Greenhouse job boards, via the public Job Board API.

Docs: https://developers.greenhouse.io/job-board.html ("Job Board data is
publicly available, so authentication is not required for any GET endpoints").
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from jobportal.http import PoliteClient
from jobportal.sources.base import (
    CrawlContext,
    Listing,
    RawJob,
    SourceAdapter,
    SourceRef,
    SourceSpec,
    clean_token,
    infer_remote,
    parse_datetime,
)
from jobportal.text import unescape_if_needed

API = "https://boards-api.greenhouse.io/v1/boards"
_HOST_RE = re.compile(r"^(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io$", re.I)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class GreenhouseAdapter(SourceAdapter):
    kind = "greenhouse"
    display_name = "Greenhouse"

    def list_jobs(self, client: PoliteClient, source: SourceRef, context: CrawlContext) -> Listing:
        response = client.get(
            f"{API}/{source.token}/jobs?content=true",
            etag=source.etag,
            last_modified=source.last_modified,
        )
        if response.not_modified:
            return Listing(not_modified=True, etag=source.etag, last_modified=source.last_modified)
        payload = response.json()
        jobs = [self._job(item, source) for item in payload.get("jobs", []) if item.get("id")]
        return Listing(jobs=jobs, etag=response.etag, last_modified=response.last_modified)

    def _job(self, item: dict[str, Any], source: SourceRef) -> RawJob:
        job_id = str(item["id"])
        location = ((item.get("location") or {}).get("name") or "").strip()
        departments = [d.get("name", "") for d in item.get("departments") or [] if d.get("name")]
        return RawJob(
            external_id=job_id,
            title=(item.get("title") or "").strip(),
            company=(item.get("company_name") or source.company_name or source.token).strip(),
            url=item.get("absolute_url")
            or f"https://job-boards.greenhouse.io/{source.token}/jobs/{job_id}",
            # The hosted application form, also for boards embedded in a company site.
            apply_url=f"https://job-boards.greenhouse.io/embed/job_app?for={source.token}&token={job_id}",
            location=location,
            remote=infer_remote(location),
            department=" / ".join(departments),
            requisition_id=str(item.get("requisition_id") or ""),
            description_html=unescape_if_needed(item.get("content") or ""),
            posted_at=parse_datetime(item.get("first_published")),
            updated_at=parse_datetime(item.get("updated_at")),
            raw={k: v for k, v in item.items() if k != "content"},
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = (parts.hostname or "").lower()
        segments = [s for s in parts.path.split("/") if s]
        token: str | None = None
        if (
            host == "boards-api.greenhouse.io"
            and len(segments) >= 3
            and segments[:2] == ["v1", "boards"]
        ):
            token = segments[2]
        elif _HOST_RE.match(host):
            if segments[:1] == ["embed"]:
                named = parse_qs(parts.query).get("for")
                token = named[0] if named else None
            elif segments:
                token = segments[0]
        if token and _TOKEN_RE.match(token):
            return SourceSpec(kind=cls.kind, token=clean_token(token))
        return None

    def board_url(self, source: SourceRef) -> str:
        return f"https://job-boards.greenhouse.io/{source.token}"
