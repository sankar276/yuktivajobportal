"""Crawl sources and keep the ``jobs`` table in step with what is posted.

Network calls run on a small thread pool (the client spaces requests per
host); all database writes happen on the calling thread.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from jobportal.comp import extract_comp
from jobportal.config import Employment
from jobportal.db import utcnow
from jobportal.http import FetchError, NotFound, PoliteClient, RobotsDisallowed
from jobportal.models import Job, Source, SourceStatus
from jobportal.sources import ADAPTERS, CrawlContext, Listing, RawJob, SourceRef, SourceSpec
from jobportal.sources.base import infer_remote, remote_from_description
from jobportal.text import company_key, html_to_text, job_fingerprint, sha256_text, squash

log = logging.getLogger(__name__)

#: A posting missing from a *queried* (incomplete) listing is closed after this long.
PARTIAL_CLOSE_AFTER = timedelta(days=7)
MAX_DETAILS_PER_SOURCE = 40

_CONTRACT_TITLE_RE = re.compile(r"\b(contract|contractor|c2c|corp[- ]to[- ]corp)\b", re.I)
_INTERN_TITLE_RE = re.compile(r"\bintern(ship)?\b", re.I)
# A description that plainly calls the role a contract.
_CONTRACT_TEXT_RE = re.compile(
    r"\b\d+\+?[- ]months?(?:\s+\w+)?\s+contract\b"
    r"|\b(?:long|short)[- ]term contract\b"
    r"|\bcontract[- ]to[- ]hire\b"
    r"|\bcontract (?:position|role|opportunity|duration|length)\b"
    r"|\b(?:c2c|corp[- ]to[- ]corp|w2 contract)\b",
    re.I,
)


@dataclass
class CrawlResult:
    source_id: int
    label: str
    status: str
    found: int = 0
    new: int = 0
    updated: int = 0
    closed: int = 0
    hydrated: int = 0
    error: str = ""


# ------------------------------------------------------------------ sources


def source_ref(source: Source) -> SourceRef:
    return SourceRef(
        id=source.id,
        kind=source.kind,
        token=source.token,
        company_name=source.company_name,
        config=dict(source.config or {}),
        etag=source.etag,
        last_modified=source.last_modified,
    )


def add_source(session: Session, spec: SourceSpec, company_name: str = "") -> tuple[Source, bool]:
    """Create the source unless it exists. Returns ``(source, created)``."""
    existing = session.scalar(
        select(Source).where(Source.kind == spec.kind, Source.token == spec.token)
    )
    if existing is not None:
        if company_name and not existing.company_name:
            existing.company_name = company_name
        return existing, False
    source = Source(
        kind=spec.kind,
        token=spec.token,
        company_name=company_name or spec.company_name,
        config=dict(spec.config),
    )
    session.add(source)
    session.flush()
    return source, True


def special_source(session: Session, kind: str) -> Source:
    """The single source row that owns email- or manually-added roles."""
    spec = SourceSpec(kind=kind, token=kind, company_name="")
    source, _created = add_source(session, spec)
    source.initialized = True  # nothing found here is a backlog
    return source


# -------------------------------------------------------------------- crawl


def crawl(
    session: Session,
    client: PoliteClient,
    *,
    source_ids: Iterable[int] | None = None,
    context: CrawlContext | None = None,
    title_filter: Callable[[str], bool] | None = None,
    min_interval: timedelta | None = None,
    now: datetime | None = None,
) -> list[CrawlResult]:
    """Fetch every enabled source and apply what changed.

    ``title_filter`` decides which listed-without-description postings are
    worth a detail request. ``min_interval`` skips sources crawled recently.
    """
    now = now or utcnow()
    context = context or CrawlContext()
    query = select(Source).where(Source.enabled.is_(True), Source.kind.in_(list(ADAPTERS)))
    if source_ids is not None:
        query = query.where(Source.id.in_(list(source_ids)))
    sources = list(session.scalars(query.order_by(Source.id)))
    if min_interval is not None:
        cutoff = now - min_interval
        sources = [s for s in sources if s.last_crawled_at is None or s.last_crawled_at <= cutoff]
    by_id = {source.id: source for source in sources}
    refs = [source_ref(source) for source in sources]
    results: list[CrawlResult] = []
    if not refs:
        return results

    def fetch(ref: SourceRef) -> Listing:
        return ADAPTERS[ref.kind].list_jobs(client, ref, context)

    workers = max(1, min(client.settings.crawl_workers, len(refs)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crawl") as pool:
        futures = {pool.submit(fetch, ref): ref for ref in refs}
        for future in as_completed(futures):
            ref = futures[future]
            source = by_id[ref.id]
            result = CrawlResult(
                source_id=source.id, label=source.label, status=SourceStatus.ok.value
            )
            source.last_crawled_at = now
            try:
                listing = future.result()
            except RobotsDisallowed as exc:
                _fail(source, result, SourceStatus.robots_blocked, str(exc))
            except NotFound as exc:
                _fail(source, result, SourceStatus.not_found, str(exc))
            except FetchError as exc:
                _fail(source, result, SourceStatus.error, str(exc))
            except Exception as exc:  # a bug in one adapter must not stop the others
                log.exception("crawl of %s crashed", source.label)
                _fail(source, result, SourceStatus.error, f"{type(exc).__name__}: {exc}")
            else:
                _apply_listing(session, source, listing, result, now)
                session.flush()
                result.hydrated = _hydrate(session, client, source, ref, title_filter, now)
                source.jobs_open = _count_open(session, source.id)
            session.commit()
            results.append(result)

    results.sort(key=lambda r: r.source_id)
    return results


def _fail(source: Source, result: CrawlResult, status: SourceStatus, message: str) -> None:
    source.last_status = status.value
    source.last_error = message[:2000]
    result.status = status.value
    result.error = message


def _count_open(session: Session, source_id: int) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.source_id == source_id, Job.closed_at.is_(None))
        )
        or 0
    )


def _apply_listing(
    session: Session, source: Source, listing: Listing, result: CrawlResult, now: datetime
) -> None:
    source.last_ok_at = now
    source.last_error = None
    if listing.not_modified:
        session.execute(
            update(Job)
            .where(Job.source_id == source.id, Job.closed_at.is_(None))
            .values(last_seen_at=now)
        )
        source.last_status = SourceStatus.unchanged.value
        result.status = SourceStatus.unchanged.value
        return

    existing = {
        job.external_id: job
        for job in session.scalars(select(Job).where(Job.source_id == source.id))
    }
    backfill = not source.initialized
    seen: set[str] = set()

    for raw in listing.jobs:
        if not raw.external_id or not raw.title or raw.external_id in seen:
            continue
        seen.add(raw.external_id)
        listing_hash = _listing_hash(raw)
        job = existing.get(raw.external_id)
        if job is None:
            job = Job(
                source_id=source.id,
                external_id=raw.external_id,
                first_seen_at=now,
                last_seen_at=now,
                is_backfill=backfill,
            )
            _fill(job, raw, source)
            job.content_hash = listing_hash
            session.add(job)
            existing[raw.external_id] = job
            result.new += 1
            continue
        job.last_seen_at = now
        if job.closed_at is not None:
            job.closed_at = None  # it came back
            result.updated += 1
        if job.content_hash != listing_hash:
            _fill(job, raw, source)
            job.content_hash = listing_hash
            result.updated += 1

    for external_id, job in existing.items():
        if external_id in seen or job.closed_at is not None:
            continue
        if listing.complete or job.last_seen_at < now - PARTIAL_CLOSE_AFTER:
            job.closed_at = now
            result.closed += 1

    result.found = len(seen)
    source.initialized = True
    source.last_status = SourceStatus.ok.value
    source.etag = listing.etag
    source.last_modified = listing.last_modified


def _listing_hash(raw: RawJob) -> str:
    return sha256_text(
        raw.title,
        raw.location,
        raw.description_html,
        raw.apply_url,
        raw.url,
        str(raw.remote),
        raw.employment_type,
        str(raw.comp_min),
        str(raw.comp_max),
    )


def _fill(job: Job, raw: RawJob, source: Source) -> None:
    """Copy a source's view of a posting onto the job row."""
    company = squash(raw.company) or source.company_name or source.token
    title = squash(raw.title)[:500]
    job.company_name = company[:200]
    job.company_key = company_key(company)[:200]
    job.title = title
    job.fingerprint = job_fingerprint(company, title)
    job.url = (raw.url or job.url or "")[:1000]
    apply_url = raw.apply_url or job.apply_url
    job.apply_url = apply_url[:1000] if apply_url else None
    job.department = squash(raw.department)[:300]
    job.requisition_id = squash(raw.requisition_id)[:120] or job.requisition_id or ""
    job.source_updated_at = raw.updated_at
    if raw.posted_at is not None and job.posted_at is None:
        job.posted_at = raw.posted_at

    if raw.needs_detail:
        # A stub: keep what a previous detail fetch filled in, but fetch again.
        if raw.location:
            job.location = squash(raw.location)[:500]
        job.location = job.location or ""
        if job.remote is None:
            job.remote = raw.remote
        job.needs_detail = True
    else:
        _fill_detail(job, raw)

    job.raw = dict(raw.raw or {})


def _fill_detail(job: Job, raw: RawJob) -> None:
    job.location = squash(raw.location)[:500]
    job.description_html = raw.description_html or ""
    job.description_text = html_to_text(job.description_html)
    remote = raw.remote if raw.remote is not None else infer_remote(job.location)
    job.remote = remote if remote is not None else remote_from_description(job.description_text)
    job.employment_type = (
        raw.employment_type
        or _employment_from_title(job.title)
        or (Employment.contract.value if _CONTRACT_TEXT_RE.search(job.description_text) else None)
    )
    job.needs_detail = False

    if raw.comp_min is not None or raw.comp_max is not None:
        job.comp_min, job.comp_max = raw.comp_min, raw.comp_max
        job.comp_currency, job.comp_period = raw.comp_currency, raw.comp_period
    else:
        found = extract_comp(job.description_text)
        if found is not None:
            job.comp_min, job.comp_max = found.minimum, found.maximum
            job.comp_currency, job.comp_period = found.currency, found.period
        else:
            job.comp_min = job.comp_max = None
            job.comp_currency = job.comp_period = None


def _employment_from_title(title: str) -> str | None:
    if _INTERN_TITLE_RE.search(title):
        return Employment.internship.value
    if _CONTRACT_TITLE_RE.search(title):
        return Employment.contract.value
    return None


def _hydrate(
    session: Session,
    client: PoliteClient,
    source: Source,
    ref: SourceRef,
    title_filter: Callable[[str], bool] | None,
    now: datetime,
) -> int:
    """Fetch descriptions for stubs worth reading, newest first."""
    adapter = ADAPTERS[source.kind]
    pending = session.scalars(
        select(Job)
        .where(Job.source_id == source.id, Job.needs_detail.is_(True), Job.closed_at.is_(None))
        .order_by(Job.first_seen_at.desc(), Job.id.desc())
    )
    hydrated = 0
    for job in pending:
        if title_filter is not None and not title_filter(job.title):
            continue
        if hydrated >= MAX_DETAILS_PER_SOURCE:
            break
        stub = RawJob(
            external_id=job.external_id,
            title=job.title,
            company=job.company_name,
            url=job.url,
            apply_url=job.apply_url,
            location=job.location,
            remote=job.remote,
            requisition_id=job.requisition_id,
            posted_at=job.posted_at,
            needs_detail=True,
            raw=dict(job.raw or {}),
        )
        try:
            detail = adapter.fetch_detail(client, ref, stub)
        except NotFound:
            job.closed_at = now
            continue
        except RobotsDisallowed as exc:
            log.info("detail for %s blocked by robots.txt: %s", source.label, exc)
            break
        except FetchError as exc:
            log.warning("detail fetch failed for %s job %s: %s", source.label, job.external_id, exc)
            continue
        if detail.needs_detail:
            continue  # the adapter could not fill it in; try again next time
        title = squash(detail.title)[:500] or job.title
        if title != job.title:
            job.title = title
            job.fingerprint = job_fingerprint(job.company_name, title)
        job.url = (detail.url or job.url)[:1000]
        job.requisition_id = squash(detail.requisition_id)[:120] or job.requisition_id
        if detail.posted_at is not None:
            job.posted_at = detail.posted_at  # the detail page has the exact date
        _fill_detail(job, detail)
        job.raw = dict(detail.raw or {})
        hydrated += 1
    return hydrated
