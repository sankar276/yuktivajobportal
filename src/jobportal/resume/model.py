"""The evidence bank: everything true you could put on a resume.

A tailored resume is a *selection and ordering* of what is in this file.
Nothing is ever added that is not here.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from jobportal.config import ConfigError, StrictModel, load_model

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _pretty_month(value: str) -> str:
    """'2019-03' -> 'Mar 2019'; anything else is shown as written."""
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", value.strip())
    if match and 1 <= int(match.group(2)) <= 12:
        return f"{_MONTHS[int(match.group(2)) - 1]} {match.group(1)}"
    return value


class Bullet(StrictModel):
    text: str
    #: What this bullet is evidence of: skills, themes ("leadership", "cost").
    tags: list[str] = Field(default_factory=list)
    #: Always include, whatever the job.
    pinned: bool = False
    #: Only use in these resume variants. Empty = any.
    variants: list[str] = Field(default_factory=list)
    id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: Any) -> Any:
        return {"text": value} if isinstance(value, str) else value

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("bullet text is empty")
        return value


class Role(StrictModel):
    company: str
    title: str
    start: str
    #: Empty = present.
    end: str = ""
    location: str = ""
    bullets: list[Bullet] = Field(default_factory=list)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> Any:
        # YAML reads a bare 2019 as an int and 2019-03 as a string; accept both.
        return "" if value is None else str(value)

    @property
    def period(self) -> str:
        return f"{_pretty_month(self.start)} - {_pretty_month(self.end) if self.end else 'Present'}"


class SkillGroup(StrictModel):
    group: str
    items: list[str]


class Education(StrictModel):
    school: str
    degree: str = ""
    year: str = ""

    @field_validator("year", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> Any:
        return "" if value is None else str(value)


class Certification(StrictModel):
    name: str
    issuer: str = ""
    year: str = ""

    @field_validator("year", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> Any:
        return "" if value is None else str(value)


class ExtraSection(StrictModel):
    """Anything else: publications, talks, community, patents."""

    title: str
    items: list[str]


class Variant(StrictModel):
    """One positioning of the same career: headline, summary and what to favour."""

    headline: str = ""
    summary: str
    #: Tags that get a boost when choosing bullets for this variant.
    emphasize: list[str] = Field(default_factory=list)


class TailorOptions(StrictModel):
    #: The most recent N roles get the larger bullet budget.
    recent_roles: int = Field(default=2, ge=0)
    max_bullets_recent: int = Field(default=6, ge=1)
    max_bullets_older: int = Field(default=3, ge=0)
    #: Roles older than this many (by position) are listed without bullets.
    detail_roles: int = Field(default=5, ge=1)


class ResumeBank(StrictModel):
    variants: dict[str, Variant]
    skills: list[SkillGroup] = Field(default_factory=list)
    experience: list[Role]
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    extras: list[ExtraSection] = Field(default_factory=list)
    options: TailorOptions = Field(default_factory=TailorOptions)

    @model_validator(mode="after")
    def _finish(self) -> ResumeBank:
        if not self.variants:
            raise ValueError("define at least one resume variant (usually 'default')")
        if not self.experience:
            raise ValueError("experience is empty")
        seen: set[str] = set()
        for role_index, role in enumerate(self.experience):
            for bullet in role.bullets:
                unknown = set(bullet.variants) - set(self.variants)
                if unknown:
                    raise ValueError(
                        f"bullet in {role.company!r} names unknown variants: {', '.join(sorted(unknown))}"
                    )
                if not bullet.id:
                    digest = hashlib.sha1(bullet.text.encode("utf-8")).hexdigest()[:8]
                    bullet.id = f"r{role_index}-{digest}"
                if bullet.id in seen:
                    raise ValueError(f"duplicate bullet id {bullet.id!r} (same text twice?)")
                seen.add(bullet.id)
        return self

    def vocabulary(self) -> list[str]:
        """Every skill and tag you can truthfully claim, de-duplicated, in file order."""
        terms: dict[str, None] = {}
        for group in self.skills:
            for item in group.items:
                terms.setdefault(item)
        for role in self.experience:
            for bullet in role.bullets:
                for tag in bullet.tags:
                    terms.setdefault(tag)
        return list(terms)


def load_resume_bank(path: Path) -> ResumeBank:
    return load_model(path, ResumeBank)


# ------------------------------------------------------ the tailored result


class TailoredRole(BaseModel):
    company: str
    title: str
    period: str
    location: str = ""
    bullets: list[str] = Field(default_factory=list)


class TailoredResume(BaseModel):
    name: str
    headline: str = ""
    contact: list[str] = Field(default_factory=list)
    summary: str = ""
    skills: list[SkillGroup] = Field(default_factory=list)
    experience: list[TailoredRole] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    extras: list[ExtraSection] = Field(default_factory=list)


__all__ = [
    "Bullet",
    "Certification",
    "ConfigError",
    "Education",
    "ExtraSection",
    "ResumeBank",
    "Role",
    "SkillGroup",
    "TailorOptions",
    "TailoredResume",
    "TailoredRole",
    "Variant",
    "load_resume_bank",
]
