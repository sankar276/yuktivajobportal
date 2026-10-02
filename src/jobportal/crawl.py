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
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, defer

from jobportal.comp import extract_comp
from jobportal.config import Employment
from jobportal.db import utcnow
from jobportal.facts import extract_facts
from jobportal.http import FetchError, NotFound, PoliteClient, RobotsDisallowed
from jobportal.models import Application, Job, Source, SourceStatus
from jobportal.sources import ADAPTERS, CrawlContext, Listing, RawJob, SourceRef, SourceSpec
from jobportal.sources.base import infer_remote, remote_from_description, web_url
from jobportal.text import company_key, html_to_text, job_fingerprint, sha256_text, squash

log = logging.getLogger(__name__)

#: A posting missing from a *queried* (incomplete) listing is closed after this long.
PARTIAL_CLOSE_AFTER = timedelta(days=7)
#: Detail requests per source per crawl: attempts, whether or not they succeed.
MAX_DETAILS_PER_SOURCE = 40
#: A failed detail request is retried after 2, 4, 8 ... hours, at most this long.
DETAIL_RETRY_MAX = timedelta(days=7)
#: Longer markup is cut before it is parsed; no posting is this long.
MAX_DESCRIPTION_CHARS = 200_000
#: A complete listing that is suddenly empty closes nothing until it is seen this often.
EMPTY_LISTINGS_BEFORE_CLOSING = 2

_INTERN_TITLE_RE = re.compile(r"\bintern(ship)?\b", re.I)
_CONTRACT_MARKERS_RE = re.compile(
    r"\b(?:c2c|corp[- ]to[- ]corp|w2 contract|contract[- ]to[- ]hire|contractor)\b", re.I
)
# What follows "Contract" in the title of a job that is *about* contracts.
_ABOUT_CONTRACTS_RE = re.compile(
    r"(?:lifecycle|life cycle|manage\w*|specialist|admin\w*|analyst|attorney|counsel|negotiat\w*"
    r"|law\w*|compliance|officer|coordinator|review\w*|draft\w*|operations|ops)\b",
    re.I,
)
# A description that plainly calls the role itself a contract.
_CONTRACT_TEXT_RE = re.compile(
    r"\b\d+\+?[- ]months?(?:\s+\w+)?\s+contract\b"
    r"|\b(?:long|short)[- ]term contract\b"
    r"|\bcontract[- ]to[- ]hire\b"
    r"|\bcontract (?:position|role|opportunity|assignment)\b"
    r"|\bcontract (?:duration|length)\s*[:\-–]"
    r"|\b(?:c2c|corp[- ]to[- ]corp|w2 contract)\b",
    re.I,
)
_NEGATION_RE = re.compile(r"\b(?:no|not|non|never|unable|cannot|without)\b|n['’]t\b", re.I)


def is_contract_title(title: str) -> bool:
    """Does the title say the role is a contract (and not a role about contracts)?"""
    lowered = title.lower()
    if re.search(r"\bsmart contracts?\b", lowered):
        return False
    if _CONTRACT_MARKERS_RE.search(lowered):
        return True
    found = re.search(r"\bcontract\b", lowered)
    if not found:
        return False
    return not _ABOUT_CONTRACTS_RE.match(lowered[found.end() :].strip(" -–,:()/"))


def is_contract_text(text: str) -> bool:
    """Does the description call the role a contract? "No C2C" and "not a contract position" do not."""
    for match in _CONTRACT_TEXT_RE.finditer(text):
        clause = re.split(r"[.;\n]", text[max(0, match.start() - 60) : match.start()])[-1]
        if not _NEGATION_RE.search(clause):
            return True
    return False


def _clean(value: Any) -> Any:
    """Strings as a database can store them: without NUL characters, at any depth."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_clean(key): _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


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
        if existing.removed_at is not None:
            # Removed earlier but kept for its application history: watch it again.
            existing.removed_at = None
            existing.enabled = True
            return existing, True
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


def remove_source(
    session: Session, source: Source, *, now: datetime | None = None
) -> tuple[int, int]:
    """Stop watching a board. Returns ``(postings_deleted, postings_kept)``.

    Postings you have an application for are kept, together with the
    application: your history, and the guard against applying to the same
    role twice, must not disappear with the board. When anything is kept the
    source row stays too, switched off and marked removed, so that adding the
    board again reconnects to those postings instead of creating new copies.
    """
    if source.kind not in ADAPTERS:
        raise ValueError("The mailbox and the roles you added yourself are not removable sources.")
    kept = set(
        session.scalars(
            select(Application.job_id)
            .join(Job, Job.id == Application.job_id)
            .where(Job.source_id == source.id)
        )
    )
    total = _count_all(session, source.id)
    if not kept:
        session.delete(source)  # takes its postings with it
        session.flush()
        return total, 0
    session.execute(
        delete(Job)
        .where(Job.source_id == source.id, Job.id.not_in(kept))
        .execution_options(synchronize_session=False)
    )
    source.enabled = False
    source.removed_at = now or utcnow()
    session.flush()
    session.expire_all()  # the bulk delete bypassed the objects loaded in this session
    return total - len(kept), len(kept)


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
    query = select(Source).where(
        Source.enabled.is_(True), Source.removed_at.is_(None), Source.kind.in_(list(ADAPTERS))
    )
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
                try:
                    # In a savepoint: whatever one board's data does, the
                    # other boards and this session carry on.
                    with session.begin_nested():
                        _apply_listing(session, source, listing, result, now)
                        session.flush()
                except Exception as exc:
                    log.exception("applying the listing of %s failed", source.label)
                    result.found = result.new = result.updated = result.closed = 0
                    _fail(source, result, SourceStatus.error, f"{type(exc).__name__}: {exc}")
                else:
                    # Stored before any detail request goes out, so no
                    # database lock is held while waiting on the network.
                    session.commit()
                    try:
                        result.hydrated = _hydrate(session, client, source, ref, title_filter, now)
                    except Exception as exc:
                        session.rollback()
                        log.exception("fetching details for %s failed", source.label)
                        _fail(source, result, SourceStatus.error, f"{type(exc).__name__}: {exc}")
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


def _count_all(session: Session, source_id: int) -> int:
    return (
        session.scalar(select(func.count()).select_from(Job).where(Job.source_id == source_id)) or 0
    )


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

    # Only what is needed to compare; the long texts are loaded for a job if
    # it turns out to have changed.
    existing = {
        job.external_id: job
        for job in session.scalars(
            select(Job)
            .where(Job.source_id == source.id)
            .options(defer(Job.description_html), defer(Job.description_text), defer(Job.raw))
        )
    }
    backfill = not source.initialized
    seen: set[str] = set()

    for raw in listing.jobs:
        if not raw.external_id or not raw.title or raw.external_id in seen:
            continue
        seen.add(raw.external_id)
        try:
            listing_hash = _listing_hash(raw)
            job = existing.get(raw.external_id)
            if job is None:
                job = Job(
                    source_id=source.id,
                    external_id=raw.external_id[:255],
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
            unchanged = job.content_hash == listing_hash
            if job.closed_at is not None and unchanged and _waiting_on_detail(job, now):
                continue  # its page answered "gone"; do not reopen it on every pass
            job.last_seen_at = now
            changed = False
            if job.closed_at is not None:
                job.closed_at = None  # it came back
                changed = True
            if not unchanged:
                _fill(job, raw, source)
                job.content_hash = listing_hash
                changed = True
            result.updated += changed
        except Exception as exc:  # one unreadable posting costs only itself
            stored = existing.get(raw.external_id)
            if stored is not None and stored in session and stored not in session.new:
                session.expire(stored)  # drop a half-applied change
            log.warning(
                "%s: posting %s could not be stored (%s: %s)",
                source.label, raw.external_id, type(exc).__name__, exc,
            )  # fmt: skip

    open_before = [job for job in existing.values() if job.closed_at is None]
    state = dict(source.config or {})
    if listing.complete and not seen and len(open_before) > 0:
        # A board that had postings and now lists none is far more often a
        # glitch than a company that closed everything at once.
        streak = int(state.get("empty_listings", 0)) + 1
        source.config = {**state, "empty_listings": streak}
        if streak < EMPTY_LISTINGS_BEFORE_CLOSING:
            source.initialized = True
            source.last_status = SourceStatus.empty.value
            source.last_error = (
                f"The board listed no postings. Its {len(open_before)} open roles are kept "
                "until the next reading says the same."
            )
            result.status = SourceStatus.empty.value
            result.error = source.last_error
            return
    elif state.get("empty_listings"):
        source.config = {key: value for key, value in state.items() if key != "empty_listings"}

    for external_id, job in existing.items():
        if external_id in seen or job.closed_at is not None:
            continue
        if listing.complete or job.last_seen_at < now - PARTIAL_CLOSE_AFTER:
            job.closed_at = now
            result.closed += 1

    result.found = len(seen)
    source.initialized = True
    source.last_status = SourceStatus.ok.value
    source.etag = listing.etag[:255] if listing.etag else None
    source.last_modified = listing.last_modified[:255] if listing.last_modified else None


def _waiting_on_detail(job: Job, now: datetime) -> bool:
    """Closed because its detail page said "gone", and not yet due for another look."""
    return bool(job.needs_detail and job.detail_retry_at is not None and job.detail_retry_at > now)


def _listing_hash(raw: RawJob) -> str:
    declared = (raw.raw or {}).get("workplaceType") or (raw.raw or {}).get("remoteType")
    return sha256_text(
        raw.title,
        raw.company,
        raw.location,
        raw.description_html,
        raw.apply_url,
        raw.url,
        str(raw.remote),
        raw.employment_type,
        str(raw.comp_min),
        str(raw.comp_max),
        raw.comp_currency,
        raw.comp_period,
        declared if isinstance(declared, str) else "",
    )


def _fill(job: Job, raw: RawJob, source: Source) -> None:
    """Copy a source's view of a posting onto the job row.

    Everything here is third-party data: text is stored without NUL
    characters and cut to its column, and links are kept only when they are
    plain web addresses.
    """
    company = squash(_clean(raw.company)) or source.company_name or source.token
    title = squash(_clean(raw.title))[:500]
    job.company_name = company[:200]
    job.company_key = company_key(company)[:200]
    job.title = title
    job.fingerprint = job_fingerprint(company, title)
    job.url = (web_url(_clean(raw.url)) or job.url or "")[:1000]
    apply_url = web_url(_clean(raw.apply_url)) or job.apply_url
    job.apply_url = apply_url[:1000] if apply_url else None
    job.department = squash(_clean(raw.department))[:300]
    job.requisition_id = squash(_clean(raw.requisition_id))[:120] or job.requisition_id or ""
    job.source_updated_at = raw.updated_at
    if raw.posted_at is not None and job.posted_at is None:
        job.posted_at = raw.posted_at

    if raw.needs_detail:
        # A stub: keep what a previous detail fetch filled in, but fetch again.
        if raw.location:
            job.location = squash(_clean(raw.location))[:500]
        job.location = job.location or ""
        if job.remote is None:
            job.remote = raw.remote
        job.needs_detail = True
        job.detail_retry_at = None  # the stub changed: worth another look now
    else:
        _fill_detail(job, raw)

    job.raw = _clean(dict(raw.raw or {}))


def _fill_detail(job: Job, raw: RawJob) -> None:
    job.location = squash(_clean(raw.location))[:500]
    job.description_html = _clean(raw.description_html or "")[:MAX_DESCRIPTION_CHARS]
    job.description_text = html_to_text(job.description_html)
    remote = raw.remote if raw.remote is not None else infer_remote(job.location)
    job.remote = remote if remote is not None else remote_from_description(job.description_text)
    job.employment_type = (
        raw.employment_type
        or _employment_from_title(job.title)
        or (Employment.contract.value if is_contract_text(job.description_text) else None)
    )
    job.needs_detail = False
    job.detail_failures = 0
    job.detail_retry_at = None
    declared = (raw.raw or {}).get("workplaceType") or (raw.raw or {}).get("remoteType")
    job.facts = extract_facts(
        job.description_text,
        remote=job.remote,
        location=job.location,
        declared_workplace=declared if isinstance(declared, str) else None,
    )
    job.workplace = job.facts.get("workplace")

    if raw.comp_min is not None or raw.comp_max is not None:
        job.comp_min, job.comp_max = raw.comp_min, raw.comp_max
        job.comp_currency = (raw.comp_currency or "")[:8].upper() or None
        job.comp_period = (raw.comp_period or "")[:16] or None
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
    if is_contract_title(title):
        return Employment.contract.value
    return None


def _retry_at(job: Job, now: datetime) -> datetime:
    """When a failed detail request may be tried again: 2, 4, 8 ... hours on."""
    hours = 2 ** min(max(job.detail_failures, 1), 12)
    return now + min(timedelta(hours=hours), DETAIL_RETRY_MAX)


def _hydrate(
    session: Session,
    client: PoliteClient,
    source: Source,
    ref: SourceRef,
    title_filter: Callable[[str], bool] | None,
    now: datetime,
) -> int:
    """Fetch descriptions for stubs worth reading, newest first.

    At most ``MAX_DETAILS_PER_SOURCE`` requests per pass, successful or not.
    A posting whose request failed is left alone for a growing while, and
    each result is committed on its own, so a slow board holds no lock.
    """
    adapter = ADAPTERS[source.kind]
    pending = session.scalars(
        select(Job)
        .where(Job.source_id == source.id, Job.needs_detail.is_(True), Job.closed_at.is_(None))
        .order_by(Job.first_seen_at.desc(), Job.id.desc())
    ).all()
    hydrated = attempts = 0
    for job in pending:
        if title_filter is not None and not title_filter(job.title):
            continue
        if job.detail_retry_at is not None and job.detail_retry_at > now:
            continue
        if attempts >= MAX_DETAILS_PER_SOURCE:
            break
        attempts += 1
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
        except RobotsDisallowed as exc:
            log.info("detail for %s blocked by robots.txt: %s", source.label, exc)
            break
        except NotFound:
            # Gone. Kept closed until the retry time even if the listing still
            # shows it, so it is not reopened and asked for again every pass.
            job.detail_failures += 1
            job.detail_retry_at = now + DETAIL_RETRY_MAX
            job.closed_at = now
            session.commit()
            continue
        except Exception as exc:  # network trouble or an answer the adapter cannot read
            log.warning("detail fetch failed for %s job %s: %s", source.label, job.external_id, exc)
            session.rollback()
            job.detail_failures += 1
            job.detail_retry_at = _retry_at(job, now)
            session.commit()
            continue
        if detail.needs_detail:
            continue  # the adapter could not fill it in; try again next time
        try:
            title = squash(_clean(detail.title))[:500] or job.title
            if title != job.title:
                job.title = title
                job.fingerprint = job_fingerprint(job.company_name, title)
            job.url = (web_url(_clean(detail.url)) or job.url)[:1000]
            job.requisition_id = squash(_clean(detail.requisition_id))[:120] or job.requisition_id
            if detail.posted_at is not None:
                job.posted_at = detail.posted_at  # the detail page has the exact date
            _fill_detail(job, detail)
            job.raw = _clean(dict(detail.raw or {}))
            session.commit()
            hydrated += 1
        except Exception as exc:
            session.rollback()
            log.warning(
                "details of %s job %s could not be stored: %s", source.label, job.external_id, exc
            )
            job.detail_failures += 1
            job.detail_retry_at = _retry_at(job, now)
            session.commit()
    return hydrated
