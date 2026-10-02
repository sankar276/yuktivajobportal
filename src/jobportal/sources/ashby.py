"""Ashby job boards, via the public Job Posting API.

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

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

API = "https://api.ashbyhq.com/posting-api/job-board"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\- ]+$")
_EMPLOYMENT = {
    "fulltime": "full_time",
    "parttime": "part_time",
    "intern": "internship",
    "contract": "contract",
    "temporary": "temporary",
}


class AshbyAdapter(SourceAdapter):
    kind = "ashby"
    display_name = "Ashby"

    def list_jobs(self, client: PoliteClient, source: SourceRef, context: CrawlContext) -> Listing:
        response = client.get(
            f"{API}/{source.token}?includeCompensation=true",
            etag=source.etag,
            last_modified=source.last_modified,
        )
        if response.not_modified:
            return Listing(not_modified=True, etag=source.etag, last_modified=source.last_modified)
        items = listed(response.json(), "jobs", source.label)
        jobs = build_jobs(items, lambda item: self._job(item, source), source.label)
        return Listing(jobs=jobs, etag=response.etag, last_modified=response.last_modified)

    def _job(self, item: dict[str, Any], source: SourceRef) -> RawJob | None:
        if not item.get("id") or not item.get("isListed", True):
            return None
        places = [text_of(item.get("location"))]
        places += [text_of(place) for place in item.get("secondaryLocations") or []]
        location = "; ".join(dict.fromkeys(p for p in places if p))
        # A declared workplace type ("Hybrid", "OnSite") outranks the remote
        # flag: boards tick "remote" for roles that are remote some days only.
        declared = item.get("workplaceType")
        remote = infer_remote("", declared) if isinstance(declared, str) else None
        if remote is None:
            flag = item.get("isRemote")
            remote = flag if isinstance(flag, bool) else infer_remote(location)
        employment = str(item.get("employmentType") or "")
        comp_min, comp_max, currency, period = _salary(item.get("compensation") or {})
        department = " / ".join(part for part in (item.get("department"), item.get("team")) if part)
        return RawJob(
            external_id=str(item["id"]),
            title=(item.get("title") or "").strip(),
            company=source.company_name or source.token,
            url=web_url(item.get("jobUrl")),
            apply_url=web_url(item.get("applyUrl")) or None,
            location=location,
            remote=bool(remote) if remote is not None else None,
            employment_type=_EMPLOYMENT.get(employment.lower()) or parse_employment(employment),
            department=department,
            description_html=item.get("descriptionHtml") or "",
            posted_at=parse_datetime(item.get("publishedAt")),
            comp_min=comp_min,
            comp_max=comp_max,
            comp_currency=currency,
            comp_period=period,
            raw={k: v for k, v in item.items() if k not in {"descriptionHtml", "descriptionPlain"}},
        )

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = (parts.hostname or "").lower()
        segments = [unquote(s) for s in parts.path.split("/") if s]
        token: str | None = None
        if host == "jobs.ashbyhq.com" and segments:
            token = segments[0]
        elif (
            host == "api.ashbyhq.com"
            and segments[:2] == ["posting-api", "job-board"]
            and len(segments) > 2
        ):
            token = segments[2]
        if token and _TOKEN_RE.match(token):
            return SourceSpec(kind=cls.kind, token=clean_token(token))
        return None

    def board_url(self, source: SourceRef) -> str:
        return f"https://jobs.ashbyhq.com/{source.token}"


def _salary(
    compensation: dict[str, Any],
) -> tuple[float | None, float | None, str | None, str | None]:
    for component in compensation.get("summaryComponents") or []:
        if component.get("compensationType") != "Salary":
            continue
        low, high = component.get("minValue"), component.get("maxValue")
        if low is None and high is None:
            continue
        interval = str(component.get("interval") or "").upper()
        period = "hour" if "HOUR" in interval else ("year" if "YEAR" in interval else None)
        if period is None:
            continue  # monthly, daily, one-off: not comparable with a yearly or hourly floor
        return (
            float(low) if low is not None else None,
            float(high) if high is not None else None,
            component.get("currencyCode"),
            period,
        )
    return None, None, None, None
