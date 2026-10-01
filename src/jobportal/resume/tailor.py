"""Cut a resume for one job from the evidence bank.

Tailoring here means *choosing and ordering*: which positioning to lead with,
which bullets earn their place for this posting, which skills go first. It
never writes a claim that is not in the bank. What the posting asks for that
the bank cannot back up is reported as a gap, not papered over.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from jobportal.config import ConfigError, Profile
from jobportal.resume.model import (
    Bullet,
    ResumeBank,
    Role,
    SkillGroup,
    TailoredResume,
    TailoredRole,
)
from jobportal.resume.vocab import COMMON_TERMS
from jobportal.text import canonical, find_terms, has_term

_HAS_NUMBER_RE = re.compile(r"\d")


@dataclass
class TailorResult:
    resume: TailoredResume
    variant: str
    #: Skills and themes from your bank that the posting asks for.
    matched: list[str] = field(default_factory=list)
    #: Things the posting asks for that your bank does not contain. Never added.
    gaps: list[str] = field(default_factory=list)
    #: What was changed relative to the full bank, in plain words.
    changes: list[str] = field(default_factory=list)
    #: ``bullet id -> text`` as selected, for later (guarded) rephrasing.
    selected: dict[str, str] = field(default_factory=dict)


def _contact(profile: Profile) -> list[str]:
    items = [profile.email, profile.phone, profile.location.display()]
    for key in ("linkedin", "github", "website", "portfolio"):
        link = profile.links.get(key)
        if link:
            items.append(re.sub(r"^https?://(www\.)?", "", link).rstrip("/"))
    return [item for item in items if item]


def _bullet_score(
    bullet: Bullet, matched_keys: set[str], matched: list[str], emphasize: set[str]
) -> float:
    tag_keys = {canonical(tag) for tag in bullet.tags}
    by_tag = tag_keys & matched_keys
    by_text = {
        canonical(term)
        for term in matched
        if canonical(term) not in by_tag and has_term(bullet.text, term)
    }
    score = 3.0 * len(by_tag) + 2.0 * len(by_text) + 1.0 * len(tag_keys & emphasize)
    if _HAS_NUMBER_RE.search(bullet.text):
        score += 0.5  # a quantified result reads stronger than a duty
    return score


def _select_bullets(
    role: Role,
    budget: int,
    variant: str,
    matched_keys: set[str],
    matched: list[str],
    emphasize: set[str],
) -> tuple[list[Bullet], list[Bullet]]:
    """``(kept, left_out)`` for one role."""
    eligible = [b for b in role.bullets if not b.variants or variant in b.variants]
    if budget <= 0:
        return [], eligible
    pinned = [b for b in eligible if b.pinned]
    others = [b for b in eligible if not b.pinned]
    order = {b.id: index for index, b in enumerate(eligible)}
    ranked = sorted(
        others,
        key=lambda b: (-_bullet_score(b, matched_keys, matched, emphasize), order[b.id]),
    )
    room = max(0, budget - len(pinned))
    kept = pinned + ranked[:room]
    return kept, ranked[room:]


def _order_skills(
    groups: list[SkillGroup], matched_keys: set[str]
) -> tuple[list[SkillGroup], list[str]]:
    """Matched skills first within each group; groups with more matches first."""
    promoted: list[str] = []
    ordered: list[tuple[int, int, SkillGroup]] = []
    for index, group in enumerate(groups):
        hits = [item for item in group.items if canonical(item) in matched_keys]
        rest = [item for item in group.items if canonical(item) not in matched_keys]
        if hits and group.items[: len(hits)] != hits:
            promoted.extend(hits)
        ordered.append((-len(hits), index, SkillGroup(group=group.group, items=hits + rest)))
    ordered.sort(key=lambda entry: (entry[0], entry[1]))
    return [group for _, _, group in ordered], promoted


def tailor(
    bank: ResumeBank,
    profile: Profile,
    *,
    title: str,
    description: str,
    variant: str = "default",
    extra_terms: Iterable[str] = (),
) -> TailorResult:
    """Build the resume for one posting. ``extra_terms`` widens gap detection."""
    if variant not in bank.variants:
        raise ConfigError(f"resume variant {variant!r} is not defined in resume.yaml")
    spec = bank.variants[variant]
    posting = f"{title}\n{description}"

    vocabulary = bank.vocabulary()
    matched = find_terms(posting, vocabulary)
    matched_keys = {canonical(term) for term in matched}
    emphasize = {canonical(tag) for tag in spec.emphasize}

    # What the posting wants that the bank cannot back up.
    bank_keys = {canonical(term) for term in vocabulary}
    bank_text = "\n".join(
        [*(v.summary for v in bank.variants.values())]
        + [b.text for role in bank.experience for b in role.bullets]
        + [c.name for c in bank.certifications]
    )
    gaps = [
        term
        for term in find_terms(posting, [*extra_terms, *COMMON_TERMS])
        if canonical(term) not in bank_keys and not has_term(bank_text, term)
    ]

    changes: list[str] = []
    if variant != "default" or len(bank.variants) > 1:
        changes.append(
            f"Positioning: '{variant}' variant" + (f" ({spec.headline})" if spec.headline else "")
        )

    options = bank.options
    experience: list[TailoredRole] = []
    selected: dict[str, str] = {}
    for index, role in enumerate(bank.experience):
        if index < options.recent_roles:
            budget = options.max_bullets_recent
        elif index < options.detail_roles:
            budget = options.max_bullets_older
        else:
            budget = 0
        kept, left_out = _select_bullets(role, budget, variant, matched_keys, matched, emphasize)
        for bullet in kept:
            selected[bullet.id] = bullet.text
        experience.append(
            TailoredRole(
                company=role.company,
                title=role.title,
                period=role.period,
                location=role.location,
                bullets=[bullet.text for bullet in kept],
            )
        )
        if left_out:
            total = len(kept) + len(left_out)
            changes.append(f"{role.company}: kept {len(kept)} of {total} bullets")

    skills, promoted = _order_skills(bank.skills, matched_keys)
    if promoted:
        changes.append("Skills moved to the front: " + ", ".join(promoted))

    certifications = sorted(
        bank.certifications,
        key=lambda cert: 0 if any(has_term(cert.name, term) for term in matched) else 1,
    )

    if matched:
        changes.append("From your bank, asked for in the posting: " + ", ".join(matched))
    if gaps:
        changes.append("Asked for but not in your bank (not added): " + ", ".join(gaps))

    resume = TailoredResume(
        name=profile.name,
        headline=spec.headline,
        contact=_contact(profile),
        summary=spec.summary,
        skills=skills,
        experience=experience,
        education=list(bank.education),
        certifications=certifications,
        extras=list(bank.extras),
    )
    return TailorResult(
        resume=resume,
        variant=variant,
        matched=matched,
        gaps=gaps,
        changes=changes,
        selected=selected,
    )
