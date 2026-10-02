"""Lever job sites, via the public Postings API.

Docs: https://github.com/lever/postings-api
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlsplit

from jobportal.http import PoliteClient
from jobportal.sources.base import (
    CrawlContext,
    Listing,
    RawJob,
    SourceAdapter,
    SourceRef,
    SourceSpec,
    build_jobs,
    clean_token,
    infer_remote,
    listed,
    parse_datetime,
    parse_employment,
    text_of,
    web_url,
)

_HOSTS = {"jobs.lever.co": "global", "jobs.eu.lever.co": "eu"}
_API = {"global": "https://api.lever.co/v0/postings", "eu": "https://api.eu.lever.co/v0/postings"}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class LeverAdapter(SourceAdapter):
    kind = "lever"
    display_name = "Lever"

    def _region(self, source: SourceRef) -> str:
        region = str(source.config.get("region", "global"))
        return region if region in _API else "global"

    def list_jobs(self, client: PoliteClient, source: SourceRef, context: CrawlContext) -> Listing:
        response = client.get(
            f"{_API[self._region(source)]}/{source.token}?mode=json",
            etag=source.etag,
            last_modified=source.last_modified,
        )
        if response.not_modified:
            return Listing(not_modified=True, etag=source.etag, last_modified=source.last_modified)
        items = listed(response.json(), None, source.label)
        jobs = build_jobs(items, lambda item: self._job(item, source), source.label)
        return Listing(jobs=jobs, etag=response.etag, last_modified=response.last_modified)

    def _job(self, item: dict[str, Any], source: SourceRef) -> RawJob | None:
        if not item.get("id"):
            return None
        categories = item.get("categories") or {}
        locations = [text_of(place) for place in categories.get("allLocations") or []]
        locations = [place for place in locations if place]
        location = "; ".join(locations) if locations else text_of(categories.get("location"))
        workplace = item.get("workplaceType")
        salary = item.get("salaryRange")
        salary = salary if isinstance(salary, dict) else {}
        interval = str(salary.get("interval") or "").lower()
        period = "hour" if "hour" in interval else ("year" if "year" in interval else None)
        if period is None:
            salary = {}  # monthly, daily, one-off: not comparable with a yearly or hourly floor
        department = " / ".join(
            part for part in (categories.get("department"), categories.get("team")) if part
        )
        return RawJob(
            external_id=str(item["id"]),
            title=(item.get("text") or "").strip(),
            company=source.company_name or source.token,
            url=web_url(item.get("hostedUrl")),
            apply_url=web_url(item.get("applyUrl")) or None,
            location=location.strip(),
            remote=infer_remote(location, workplace),
            employment_type=parse_employment(categories.get("commitment")),
            department=department,
            description_html=_description(item),
            posted_at=parse_datetime(item.get("createdAt")),
            comp_min=_number(salary.get("min")),
            comp_max=_number(salary.get("max")),
            comp_currency=salary.get("currency") if salary else None,
            comp_period=period if salary else None,
            raw={
                k: v
                for k, v in item.items()
                if k
                not in {
                    "description",
                    "descriptionPlain",
                    "descriptionBody",
                    "descriptionBodyPlain",
                    "additional",
                    "additionalPlain",
                    "opening",
                    "openingPlain",
                    "lists",
                }
            },
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = (parts.hostname or "").lower()
        segments = [s for s in parts.path.split("/") if s]
        region: str | None = _HOSTS.get(host)
        token: str | None = segments[0] if region and segments else None
        if host in ("api.lever.co", "api.eu.lever.co") and segments[:2] == ["v0", "postings"]:
            region = "eu" if ".eu." in host else "global"
            token = segments[2] if len(segments) > 2 else None
        if region and token and _TOKEN_RE.match(token):
            config = {"region": "eu"} if region == "eu" else {}
            return SourceSpec(kind=cls.kind, token=clean_token(token), config=config)
        return None

    def board_url(self, source: SourceRef) -> str:
        host = "jobs.eu.lever.co" if self._region(source) == "eu" else "jobs.lever.co"
        return f"https://{host}/{source.token}"


def _description(item: dict[str, Any]) -> str:
    parts = [item.get("description") or ""]
    for section in item.get("lists") or []:
        heading = html.escape(str(section.get("text") or ""))
        parts.append(f"<h3>{heading}</h3><ul>{section.get('content') or ''}</ul>")
    parts.append(item.get("additional") or "")
    return "\n".join(part for part in parts if part)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
