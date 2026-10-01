"""Workday career sites.

Workday has no documented public API. Its career sites load postings from a
JSON endpoint under ``/wday/cxs/``; we read the same endpoint the page does,
subject to the tenant's robots.txt like every other request.

Large tenants list thousands of roles, so the listing is *queried* with the
search terms from your lanes rather than paged in full, and descriptions are
fetched only for titles that match a lane. When the query endpoint is not
available the sitemap the site advertises is used instead.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from jobportal.db import utcnow
from jobportal.http import FetchError, NotFound, PoliteClient, RobotsDisallowed
from jobportal.sources.base import (
    CrawlContext,
    Listing,
    RawJob,
    SourceAdapter,
    SourceRef,
    SourceSpec,
    infer_remote,
    parse_datetime,
    parse_employment,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 20
MAX_PAGES_PER_TERM = 5
MAX_SITEMAP_JOBS = 400
_JOBS_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9_-]+)\.wd\d+\.myworkdayjobs\.com$", re.I)
_SITE_HOST_RE = re.compile(r"^wd\d+\.myworkdaysite\.com$", re.I)
_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_POSTED_RE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.I)
_REQ_RE = re.compile(r"_([A-Za-z]{0,4}-?\d[\w-]*)$")


class WorkdayAdapter(SourceAdapter):
    kind = "workday"
    display_name = "Workday"

    # token: "<host>/<tenant>/<site>"
    @staticmethod
    def _parts(source: SourceRef) -> tuple[str, str, str]:
        host, tenant, site = source.token.split("/", 2)
        return host, tenant, site

    def _api(self, source: SourceRef) -> str:
        host, tenant, site = self._parts(source)
        return f"https://{host}/wday/cxs/{tenant}/{site}"

    def board_url(self, source: SourceRef) -> str:
        host, tenant, site = self._parts(source)
        if _SITE_HOST_RE.match(host):
            return f"https://{host}/recruiting/{tenant}/{site}"
        return f"https://{host}/{site}"

    # -------------------------------------------------------------- listing

    def list_jobs(self, client: PoliteClient, source: SourceRef, context: CrawlContext) -> Listing:
        terms = tuple(source.config.get("search") or context.search_terms) or ("",)
        try:
            jobs = self._query(client, source, terms)
        except (RobotsDisallowed, NotFound):
            raise
        except FetchError as exc:
            log.info("workday query failed for %s (%s); trying the sitemap", source.label, exc)
            jobs = self._sitemap(client, source)
        # A queried listing is never the full set of open roles.
        return Listing(jobs=jobs, complete=False)

    def _query(
        self, client: PoliteClient, source: SourceRef, terms: tuple[str, ...]
    ) -> list[RawJob]:
        found: dict[str, RawJob] = {}
        for term in terms:
            for page in range(MAX_PAGES_PER_TERM):
                response = client.post_json(
                    f"{self._api(source)}/jobs",
                    {
                        "appliedFacets": {},
                        "limit": PAGE_SIZE,
                        "offset": page * PAGE_SIZE,
                        "searchText": term,
                    },
                )
                postings = response.json().get("jobPostings") or []
                for item in postings:
                    job = self._stub(item, source)
                    if job is not None:
                        found.setdefault(job.external_id, job)
                if len(postings) < PAGE_SIZE:
                    break
        return list(found.values())

    def _stub(self, item: dict[str, Any], source: SourceRef) -> RawJob | None:
        path = item.get("externalPath")
        title = (item.get("title") or "").strip()
        if not path or not title:
            return None
        bullets = item.get("bulletFields") or []
        requisition = str(bullets[0]) if bullets else _requisition_from_path(path)
        location = (item.get("locationsText") or "").strip()
        return RawJob(
            external_id=requisition or path.rsplit("/", 1)[-1],
            title=title,
            company=source.company_name or self._parts(source)[1],
            url=f"{self.board_url(source)}{path}",
            apply_url=f"{self.board_url(source)}{path}/apply",
            location="" if re.fullmatch(r"\d+ Locations", location) else location,
            remote=infer_remote(location),
            requisition_id=requisition,
            posted_at=_posted_on(item.get("postedOn")),
            needs_detail=True,
            raw={"externalPath": path, "postedOn": item.get("postedOn")},
        )

    def _sitemap(self, client: PoliteClient, source: SourceRef) -> list[RawJob]:
        response = client.get(f"{self.board_url(source)}/siteMap.xml")
        try:
            root = ElementTree.fromstring(response.text)
        except ElementTree.ParseError as exc:
            raise FetchError(f"{source.label}: sitemap is not valid XML: {exc}") from exc
        jobs: dict[str, RawJob] = {}
        for element in root.iter():
            if not element.tag.endswith("loc") or not element.text:
                continue
            path = urlsplit(element.text.strip()).path
            marker = path.find("/job/")
            if marker < 0:
                continue
            external_path = path[marker:]
            slug = unquote(external_path.rsplit("/", 1)[-1])
            requisition = _requisition_from_path(external_path)
            title = _title_from_slug(slug)
            job = RawJob(
                external_id=requisition or slug,
                title=title,
                company=source.company_name or self._parts(source)[1],
                url=f"{self.board_url(source)}{external_path}",
                apply_url=f"{self.board_url(source)}{external_path}/apply",
                requisition_id=requisition,
                needs_detail=True,
                raw={"externalPath": external_path, "from": "sitemap"},
            )
            jobs.setdefault(job.external_id, job)
            if len(jobs) >= MAX_SITEMAP_JOBS:
                break
        return list(jobs.values())

    # --------------------------------------------------------------- detail

    def fetch_detail(self, client: PoliteClient, source: SourceRef, job: RawJob) -> RawJob:
        path = job.raw.get("externalPath")
        if not path:
            return job
        payload = client.get(f"{self._api(source)}{path}").json()
        info = payload.get("jobPostingInfo") or {}
        places = [info.get("location") or "", *(info.get("additionalLocations") or [])]
        location = "; ".join(dict.fromkeys(p.strip() for p in places if p and p.strip()))
        job.title = (info.get("title") or job.title).strip()
        job.description_html = info.get("jobDescription") or ""
        job.location = location or job.location
        job.remote = infer_remote(job.location, info.get("remoteType"))
        job.employment_type = parse_employment(info.get("timeType"))
        job.requisition_id = str(info.get("jobReqId") or job.requisition_id)
        job.url = info.get("externalUrl") or job.url
        job.posted_at = parse_datetime(info.get("startDate")) or job.posted_at
        job.needs_detail = False
        job.raw = {**job.raw, **{k: v for k, v in info.items() if k != "jobDescription"}}
        return job

    # ------------------------------------------------------------ detection

    @classmethod
    def detect(cls, url: str) -> SourceSpec | None:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = (parts.hostname or "").lower()
        segments = [s for s in parts.path.split("/") if s]
        if segments and _LOCALE_RE.match(segments[0]):
            segments = segments[1:]
        match = _JOBS_HOST_RE.match(host)
        if match and segments[:2] == ["wday", "cxs"] and len(segments) >= 4:
            tenant, site = segments[2], segments[3]
        elif match and segments:
            tenant, site = match.group("tenant"), segments[0]
        elif _SITE_HOST_RE.match(host) and segments[:1] == ["recruiting"] and len(segments) >= 3:
            tenant, site = segments[1], segments[2]
        else:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tenant) or not re.fullmatch(r"[A-Za-z0-9_-]+", site):
            return None
        return SourceSpec(kind=cls.kind, token=f"{host}/{tenant}/{site}")


def _requisition_from_path(path: str) -> str:
    match = _REQ_RE.search(path.rsplit("/", 1)[-1])
    return match.group(1) if match else ""


def _title_from_slug(slug: str) -> str:
    """'Senior-Architect---Data-Center_JR123' -> 'Senior Architect - Data Center'."""
    words = _REQ_RE.sub("", slug).replace("---", "\x00").replace("-", " ").replace("\x00", " - ")
    return re.sub(r"\s+", " ", words).strip()


def _posted_on(value: str | None, now: datetime | None = None) -> datetime | None:
    """Workday only says "Posted Today" / "Posted 3 Days Ago"; make that a date."""
    if not value:
        return None
    now = now or utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    text = value.strip().lower()
    if "today" in text:
        return today
    if "yesterday" in text:
        return today - timedelta(days=1)
    match = _POSTED_RE.search(text)
    if match:
        return today - timedelta(days=int(match.group(1)))
    return None
