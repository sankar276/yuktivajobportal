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
    build_jobs,
    infer_remote,
    listed,
    parse_datetime,
    parse_employment,
    text_of,
    web_url,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 20
MAX_PAGES_PER_TERM = 5
#: How many of your titles are searched for, target titles first.
MAX_SEARCH_TERMS = 25
MAX_SITEMAP_JOBS = 400
_JOBS_HOST_RE = re.compile(r"^(?P<tenant>[a-z0-9_-]+)\.wd\d+\.myworkdayjobs\.com$", re.I)
_SITE_HOST_RE = re.compile(r"^wd\d+\.myworkdaysite\.com$", re.I)
_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_POSTED_RE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.I)
_REQ_RE = re.compile(r"_([A-Za-z]{0,4}-?\d[\w-]*)$")
_REQ_BULLET_RE = re.compile(r"[A-Za-z]{0,6}[-_ ]?\d[\w-]*")


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
        terms = tuple(source.config.get("search") or context.search_terms)[:MAX_SEARCH_TERMS]
        try:
            jobs = self._query(client, source, terms or ("",))
        except (RobotsDisallowed, NotFound):
            raise
        except FetchError as exc:
            log.info("workday query failed for %s (%s); trying the sitemap", source.label, exc)
            try:
                jobs = self._sitemap(client, source)
            except FetchError as fallback:
                # The sitemap was only a second try: what went wrong is the query.
                raise exc from fallback
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
                postings = listed(response.json(), "jobPostings", source.label)
                for job in build_jobs(
                    postings, lambda item: self._stub(item, source), source.label
                ):
                    found.setdefault(job.external_id, job)
                if len(postings) < PAGE_SIZE:
                    break
        return list(found.values())

    def _stub(self, item: dict[str, Any], source: SourceRef) -> RawJob | None:
        path = item.get("externalPath")
        title = (item.get("title") or "").strip()
        if not isinstance(path, str) or not path or not title:
            return None
        # The bullets are free-form ("Full time", a requisition number, both);
        # only one that looks like a requisition number is taken for one.
        bullets = [str(bullet).strip() for bullet in item.get("bulletFields") or []]
        requisition = next(
            (bullet for bullet in bullets if _REQ_BULLET_RE.fullmatch(bullet)), ""
        ) or _requisition_from_path(path)
        location = (item.get("locationsText") or "").strip()
        return RawJob(
            external_id=_external_id(path),
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
                external_id=_external_id(external_path),
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
        info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            raise FetchError(f"{source.label}: the posting's details were not in the answer")
        places = [text_of(info.get("location"))]
        places += [text_of(place) for place in info.get("additionalLocations") or []]
        location = "; ".join(dict.fromkeys(place for place in places if place))
        job.title = str(info.get("title") or job.title).strip()
        job.description_html = str(info.get("jobDescription") or "")
        job.location = location or job.location
        remote_type = info.get("remoteType")
        job.remote = infer_remote(
            job.location, remote_type if isinstance(remote_type, str) else None
        )
        job.employment_type = parse_employment(text_of(info.get("timeType")) or None)
        job.requisition_id = str(info.get("jobReqId") or job.requisition_id)
        job.url = web_url(info.get("externalUrl")) or job.url
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


def _external_id(path: str) -> str:
    """The posting's own slug: the last part of its path, the same from any listing.

    Unique per posting on a site, unlike a requisition number (several
    postings can share one) or a bullet line (which may just say "Full time").
    """
    return unquote(path.rstrip("/").rsplit("/", 1)[-1])


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
