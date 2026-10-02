"""Where an application answer comes from.

In order: your profile (for fields it recognises), an answer you gave before
(the answer bank), then your standard answers. If none of those has it, the
field is unanswered and the application waits for you. There is no fourth
source.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.apply.forms.fields import (
    DECLINE,
    LEGAL_KINDS,
    FieldKind,
    FormField,
    Resolution,
    classify,
    match_option,
    profile_value,
    standard_answer,
)
from jobportal.config import Profile
from jobportal.models import Answer
from jobportal.text import question_key, squash

_EEO = {FieldKind.eeo_gender, FieldKind.eeo_race, FieldKind.eeo_veteran, FieldKind.eeo_disability}


class AnswerBook:
    def __init__(
        self,
        profile: Profile,
        stored: dict[str, str] | None = None,
        resume_path: Path | None = None,
    ):
        self.profile = profile
        self.stored = stored or {}
        self.resume_path = resume_path

    def kind_of(self, form_field: FormField) -> FieldKind:
        return classify(form_field, self.profile)

    def resolve(self, form_field: FormField) -> Resolution | None:
        """The answer for a field, or ``None`` when you have not provided one."""
        kind = self.kind_of(form_field)

        if kind is FieldKind.resume:
            if self.resume_path is None:
                return None
            return Resolution(value=str(self.resume_path), source="tailored resume")
        if form_field.type == "file":
            return None  # cover letters and other uploads are not generated

        candidates: list[tuple[str, str]] = []
        if kind is not FieldKind.question:
            value = profile_value(kind, self.profile)
            # A legal question is answered from the profile only by picking a
            # plain Yes or No; it is never typed into a free-text box.
            if value and not (kind in LEGAL_KINDS and not form_field.options):
                candidates.append((value, "profile"))
        stored = self.stored.get(form_field.key)
        if stored:
            candidates.append((stored, "answer bank"))
        standard = standard_answer(form_field.label, self.profile)
        if standard:
            candidates.append((standard, "standard answer"))

        for value, source in candidates:
            if not form_field.options:
                if kind in _EEO and value == DECLINE:
                    continue  # "decline" only means something as a choice
                return Resolution(value=value, source=source)
            if form_field.type == "checkbox" and len(form_field.options) > 1:
                picked = _match_many(value, form_field.options)
                if picked:
                    return Resolution(
                        value="; ".join(o["label"] for o in picked), source=source, option=picked[0]
                    )
                continue
            option = match_option(value, form_field.options)
            if option is not None:
                return Resolution(value=option["label"], source=source, option=option)
        return None

    def options_for(self, form_field: FormField, resolution: Resolution) -> list[dict[str, str]]:
        """Every option to tick for a multi-select checkbox group."""
        if form_field.type == "checkbox" and len(form_field.options) > 1:
            return _match_many(resolution.value, form_field.options)
        return [resolution.option] if resolution.option else []


def _match_many(value: str, options: list[dict[str, str]]) -> list[dict[str, str]]:
    picked = []
    for part in value.split(";"):
        option = match_option(part, options)
        if option is None:
            return []  # one unmatched part makes the whole answer unreliable
        if option not in picked:
            picked.append(option)
    return picked


# ---------------------------------------------------------------- the bank


def load_answers(session: Session, user_id: int) -> dict[str, str]:
    rows = session.scalars(select(Answer).where(Answer.user_id == user_id))
    return {row.question_key: row.answer for row in rows}


def save_answer(session: Session, user_id: int, question: str, answer: str) -> Answer | None:
    """Store (or replace) your answer to a question. Blank answers are not stored."""
    key = question_key(question)
    answer = answer.strip()
    if not key or not answer:
        return None
    row = session.scalar(
        select(Answer).where(Answer.user_id == user_id, Answer.question_key == key)
    )
    if row is None:
        row = Answer(
            user_id=user_id, question_key=key, question_text=squash(question), answer=answer
        )
        session.add(row)
    else:
        row.answer = answer
        row.question_text = squash(question)
    session.flush()
    return row


def count_uses(session: Session, user_id: int, keys: list[str]) -> None:
    if not keys:
        return
    for row in session.scalars(
        select(Answer).where(Answer.user_id == user_id, Answer.question_key.in_(keys))
    ):
        row.uses += 1
