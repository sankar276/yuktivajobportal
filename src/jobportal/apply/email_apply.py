"""Write the application email for a role that is applied to by mail.

The text is assembled from your profile and the tailored resume: what you are,
which of your skills the posting asks for, and the logistics a vendor needs.
Nothing is stated that your profile does not say. A rate is quoted only if you
put one in the profile; work authorisation only if you answered both questions.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from jobportal.config import Employment, Profile
from jobportal.models import Job, ResumeVariant
from jobportal.text import canonical, squash

_ENGAGEMENT_LABELS = {"c2c": "C2C", "w2": "W2", "1099": "1099", "fte": "full-time"}
MAX_SKILLS_IN_PITCH = 6


@dataclass
class EmailDraft:
    to: str
    subject: str
    body: str
    attachments: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    references: str = ""

    def to_prepared(self) -> dict[str, Any]:
        return {"channel": "email", **asdict(self)}


_NAME_RE = re.compile(r"[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?")
_NOT_NAMES = {
    "recruiting", "recruitment", "recruiter", "team", "talent", "hiring", "careers", "jobs",
    "info", "admin", "support", "sales", "staffing", "human", "resources", "noreply", "the",
}  # fmt: skip


def _greeting(job: Job) -> str:
    """Greet by first name only when the contact really looks like a person's name."""
    first = squash(job.contact_name).split(" ")[0] if job.contact_name else ""
    if _NAME_RE.fullmatch(first) and first.lower() not in _NOT_NAMES:
        return f"Hi {first},"
    return "Hello,"


def _subject(job: Job, profile: Profile) -> str:
    original = squash((job.raw or {}).get("subject"))
    if original:
        return original if re.match(r"re:", original, re.IGNORECASE) else f"Re: {original}"
    requisition = f" ({job.requisition_id})" if job.requisition_id else ""
    return f"{job.title}{requisition} - {profile.name}"


def _pitch(job: Job, profile: Profile, variant: ResumeVariant) -> str:
    headline = squash((variant.content or {}).get("headline")) or profile.current_title
    opener = "I am interested in the"
    role = f"{job.title} role"
    if job.client_name:
        role += f" with {job.client_name}"
    sentences = [f"{opener} {role}."]

    if headline:
        article = "an" if headline[0].lower() in "aeiou" else "a"
        if profile.years_experience:
            sentences.append(
                f"I am {article} {headline} with {profile.years_experience} years of experience."
            )
        else:
            sentences.append(f"I work as {article} {headline}.")

    # Only real skills from the resume's skill groups, in the posting's order of mention.
    listed = {
        canonical(item)
        for group in (variant.content or {}).get("skills", [])
        for item in group.get("items", [])
    }
    relevant = [term for term in (variant.matched or []) if canonical(term) in listed]
    if relevant:
        shown = ", ".join(relevant[:MAX_SKILLS_IN_PITCH])
        sentences.append(f"From the requirements, my strongest areas are {shown}.")
    return " ".join(sentences)


def _logistics(job: Job, profile: Profile) -> list[str]:
    """What a staffing vendor asks for in the first reply. Only for contract roles."""
    if job.employment_type != Employment.contract.value:
        return []
    lines: list[str] = []
    terms = profile.contract
    if terms.availability:
        lines.append(f"Availability: {terms.availability}")
    if profile.location.display():
        lines.append(f"Location: {profile.location.display()}")
    if terms.engagements:
        labels = [_ENGAGEMENT_LABELS.get(e.lower(), e) for e in terms.engagements]
        lines.append("Engagement: " + " or ".join(labels))
    auth = profile.work_authorization
    if auth.authorized is True and auth.needs_sponsorship is False:
        lines.append(
            f"Work authorization: authorized to work in the {auth.country}; no sponsorship needed"
        )
    if terms.rate:
        lines.append(f"Rate: {terms.rate}")
    return lines


def compose(
    job: Job, profile: Profile, variant: ResumeVariant, *, attach_docx: bool = False
) -> EmailDraft:
    paragraphs = [_greeting(job), _pitch(job, profile, variant)]
    logistics = _logistics(job, profile)
    if logistics:
        paragraphs.append("\n".join(logistics))
    paragraphs.append("My resume is attached. I am happy to share more detail or set up a call.")
    paragraphs.append("Regards,\n" + profile.email_signature())

    attachments = [variant.pdf_path] if variant.pdf_path else []
    if attach_docx and variant.docx_path:
        attachments.append(variant.docx_path)
    message_id = squash((job.raw or {}).get("message_id"))
    return EmailDraft(
        to=(job.contact_email or "").strip(),
        subject=_subject(job, profile),
        body="\n\n".join(paragraphs) + "\n",
        attachments=[a for a in attachments if a],
        in_reply_to=message_id,
        references=squash((job.raw or {}).get("references")) or message_id,
    )
