"""Roles you add yourself: a posting you found, or a requirement a recruiter sent you."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from jobportal.comp import extract_comp
from jobportal.config import EMAIL_RE, Employment
from jobportal.crawl import special_source
from jobportal.db import utcnow
from jobportal.facts import extract_facts
from jobportal.models import Job, SourceKind
from jobportal.sources import detect_source
from jobportal.sources.base import infer_remote, remote_from_description
from jobportal.text import company_key, job_fingerprint, normalize_text, sha256_text, squash


class ManualJobError(ValueError):
    """The details given for a manually added role are not usable."""


def hosted_form_url(url: str) -> str | None:
    """The application-form address for a posting on a supported ATS, if it is one."""
    spec = detect_source(url)
    if spec is None:
        return None
    parts = urlsplit(url if "://" in url else f"https://{url}")
    segments = [s for s in parts.path.split("/") if s]
    base = f"https://{parts.netloc}/" + "/".join(segments)
    if spec.kind == SourceKind.greenhouse.value:
        match = re.search(r"/jobs/(\d+)", parts.path)
        if match:
            return f"https://job-boards.greenhouse.io/embed/job_app?for={spec.token}&token={match.group(1)}"
        return None
    if spec.kind == SourceKind.lever.value and len(segments) >= 2:
        return base if segments[-1] == "apply" else f"{base}/apply"
    if spec.kind == SourceKind.ashby.value and len(segments) >= 2:
        return base if segments[-1] == "application" else f"{base}/application"
    return None


def add_manual_job(
    session: Session,
    *,
    title: str,
    company: str,
    description: str = "",
    url: str = "",
    apply_url: str = "",
    location: str = "",
    employment_type: str | None = None,
    contact_name: str = "",
    contact_email: str = "",
    client_name: str = "",
    via_vendor: bool = False,
    now: datetime | None = None,
) -> Job:
    """Add one role by hand. With a contact email it is applied to by mail."""
    now = now or utcnow()
    title, company = squash(title), squash(company)
    if not title or not company:
        raise ManualJobError("A title and a company are required.")
    url, apply_url, contact_email = url.strip(), apply_url.strip(), contact_email.strip()
    for label, value in (("The posting link", url), ("The application link", apply_url)):
        if value and urlsplit(value).scheme not in ("http", "https"):
            raise ManualJobError(f"{label} must start with http:// or https://")
    if contact_email and not EMAIL_RE.match(contact_email):
        raise ManualJobError(f"Not an email address: {contact_email}")
    if employment_type and employment_type not in {e.value for e in Employment}:
        raise ManualJobError(f"Unknown employment type: {employment_type}")

    text = normalize_text(description)
    remote = infer_remote(location)
    if remote is None:
        remote = remote_from_description(text)
    comp = extract_comp(text)
    source = special_source(session, SourceKind.manual.value)
    job = Job(
        source_id=source.id,
        external_id=sha256_text(url or f"{company}|{title}|{now.isoformat()}")[:40],
        company_name=company[:200],
        company_key=company_key(company)[:200],
        title=title[:500],
        fingerprint=job_fingerprint(client_name or company, title),
        location=squash(location)[:500],
        remote=remote,
        employment_type=employment_type or (Employment.contract.value if via_vendor else None),
        description_text=text,
        url=url[:1000],
        apply_url=(apply_url or hosted_form_url(url) or "")[:1000] or None,
        posted_at=now,
        first_seen_at=now,
        last_seen_at=now,
        comp_min=comp.minimum if comp else None,
        comp_max=comp.maximum if comp else None,
        comp_currency=comp.currency if comp else None,
        comp_period=comp.period if comp else None,
        contact_name=squash(contact_name)[:200],
        contact_email=contact_email[:320] or None,
        client_name=squash(client_name)[:200],
        facts=extract_facts(text, remote=remote, location=location),
        raw={"vendor": bool(via_vendor)},
    )
    job.workplace = job.facts.get("workplace")
    session.add(job)
    session.flush()
    return job
