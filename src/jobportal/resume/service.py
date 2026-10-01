"""Build, store and reuse the tailored resume for a job."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from playwright.sync_api import Browser
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.config import UserConfig
from jobportal.llm import LLM, rephrase_bullets
from jobportal.models import Job, ResumeVariant, User
from jobportal.resume.render import render_html, resume_basename, write_docx, write_pdf
from jobportal.resume.tailor import TailorResult, tailor
from jobportal.settings import Settings
from jobportal.text import sha256_text


def _apply_rewrites(result: TailorResult, accepted: dict[str, str]) -> None:
    replacements = {result.selected[bullet_id]: text for bullet_id, text in accepted.items()}
    for role in result.resume.experience:
        role.bullets = [replacements.get(text, text) for text in role.bullets]


def build_resume(
    session: Session,
    settings: Settings,
    config: UserConfig,
    user: User,
    job: Job,
    *,
    variant: str = "default",
    extra_terms: Iterable[str] = (),
    browser: Browser | None = None,
    llm: LLM | None = None,
) -> ResumeVariant:
    """Tailor, render and record the resume for ``job``.

    Identical content is rendered once: a second call returns the stored
    variant. Each distinct version keeps its own files, so what was actually
    sent with an application stays on disk unchanged.
    """
    result = tailor(
        config.resume,
        config.profile,
        title=job.title,
        description=job.description_text or "",
        variant=variant,
        extra_terms=extra_terms,
    )
    if llm is not None and llm.enabled and settings.llm_rephrase:
        accepted, notes = rephrase_bullets(
            llm,
            result.selected,
            title=job.title,
            description=job.description_text or "",
            vocabulary=config.resume.vocabulary(),
        )
        _apply_rewrites(result, accepted)
        result.changes.extend(notes)

    content = result.resume.model_dump()
    content_hash = sha256_text(json.dumps(content, sort_keys=True, ensure_ascii=False))

    existing = session.scalar(
        select(ResumeVariant)
        .where(
            ResumeVariant.user_id == user.id,
            ResumeVariant.job_id == job.id,
            ResumeVariant.content_hash == content_hash,
        )
        .order_by(ResumeVariant.id.desc())
    )
    if existing is not None and existing.pdf_path and Path(existing.pdf_path).exists():
        return existing

    directory = settings.resumes_dir / f"job-{job.id}" / content_hash[:10]
    base = resume_basename(config.profile.name)
    pdf_path = write_pdf(
        render_html(result.resume), directory / f"{base}.pdf", settings=settings, browser=browser
    )
    docx_path = write_docx(result.resume, directory / f"{base}.docx")

    row = existing or ResumeVariant(user_id=user.id, job_id=job.id)
    row.variant = variant
    row.pdf_path = str(pdf_path)
    row.docx_path = str(docx_path)
    row.content = content
    row.changes = result.changes
    row.matched = result.matched
    row.gaps = result.gaps
    row.content_hash = content_hash
    session.add(row)
    session.flush()
    return row
