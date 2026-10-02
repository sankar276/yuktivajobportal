"""Bring mail into the pipeline: new requirements become jobs, replies move the tracker."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobportal.apply import ledger
from jobportal.apply.policy import WENT_OUT
from jobportal.apply.service import add_event
from jobportal.config import UserConfig
from jobportal.crawl import special_source
from jobportal.db import utcnow
from jobportal.facts import extract_facts
from jobportal.inbox.imap import Fetched, fetch_new
from jobportal.inbox.parse import (
    ParsedMail,
    extract_requirement,
    looks_like_requirement,
    parse_message,
)
from jobportal.models import (
    Application,
    AppStatus,
    InboundEmail,
    Job,
    OutboundEmail,
    SourceKind,
    User,
)
from jobportal.settings import Settings
from jobportal.text import company_key, job_fingerprint, sha256_text, squash

log = logging.getLogger(__name__)

Fetcher = Callable[..., Fetched]


@dataclass
class InboxStats:
    read: int = 0
    requirements: int = 0
    replies: int = 0
    bounces: int = 0
    ignored: int = 0

    def line(self) -> str:
        return (
            f"Read {self.read} messages: {self.requirements} new requirements, "
            f"{self.replies} replies, {self.bounces} bounces, {self.ignored} ignored."
        )


def _our_message(session: Session, mail: ParsedMail) -> OutboundEmail | None:
    """The email of ours that this message answers, if any."""
    ids = [value for value in [mail.in_reply_to, *mail.references] if value]
    if ids:
        found = session.scalars(
            select(OutboundEmail).where(
                OutboundEmail.message_id.in_(ids), OutboundEmail.status.in_(WENT_OUT)
            )
        ).first()
        if found is not None:
            return found
    if mail.is_bounce:
        # Bounces quote the original instead of threading to it.
        for outbound in session.scalars(
            select(OutboundEmail)
            .where(OutboundEmail.status.in_(WENT_OUT))
            .order_by(OutboundEmail.id.desc())
            .limit(200)
        ):
            if outbound.message_id and outbound.message_id in mail.raw_text:
                return outbound
    return None


def _record(session: Session, user: User, mail: ParsedMail, kind: str, **links: int | None) -> None:
    session.add(
        InboundEmail(
            user_id=user.id,
            message_id=mail.message_id[:255],
            in_reply_to=mail.in_reply_to[:255],
            from_addr=mail.from_addr[:320],
            from_name=mail.from_name[:200],
            subject=mail.subject[:500],
            received_at=mail.date,
            body_text=mail.text,
            kind=kind,
            **links,
        )
    )


def process_message(
    session: Session,
    config: UserConfig,
    user: User,
    mail: ParsedMail,
    stats: InboxStats,
    now: datetime,
    *,
    own_addresses: Iterable[str] = (),
) -> None:
    """File one message. ``own_addresses`` are further addresses you send from."""
    own = {config.profile.email.lower(), *(a.lower() for a in own_addresses if a)}
    if not mail.message_id or mail.from_addr in own:
        stats.ignored += 1  # our own blind copies come back through the inbox
        return
    if session.scalar(
        select(OutboundEmail.id).where(OutboundEmail.message_id == mail.message_id[:255])
    ):
        stats.ignored += 1  # something we sent ourselves, whatever address it shows
        return
    if session.scalar(
        select(InboundEmail.id).where(
            InboundEmail.user_id == user.id, InboundEmail.message_id == mail.message_id[:255]
        )
    ):
        stats.ignored += 1
        return

    ours = _our_message(session, mail)
    if ours is not None and ours.application_id is not None:
        application = session.get(Application, ours.application_id)
        if application is not None:
            if mail.is_bounce:
                # Only an application still waiting on that email goes back to
                # "did not go through". One that has moved on (a reply, an
                # interview) stays where you put it; the bounce is just noted.
                if application.status in (
                    AppStatus.submitted.value,
                    AppStatus.unconfirmed.value,
                ):
                    application.status = AppStatus.failed.value
                    application.error = f"The email to {ours.to_addr} bounced: {mail.subject}"
                    application.follow_up_at = None
                    ours.status = "bounced"
                add_event(application, "bounced", subject=mail.subject)
                _record(session, user, mail, "bounce", application_id=application.id)
                stats.bounces += 1
                return
            if mail.auto_submitted:
                add_event(application, "auto_reply", subject=mail.subject)
                _record(session, user, mail, "auto_reply", application_id=application.id)
                stats.ignored += 1
                return
            if application.status in (AppStatus.submitted.value, AppStatus.unconfirmed.value):
                if application.status == AppStatus.unconfirmed.value:
                    # A reply settles it: the message did arrive.
                    application.error = ""
                    ledger.confirm(session, application)
                    if ours.status == "unknown":
                        ours.status = "sent"
                application.status = AppStatus.replied.value
                application.follow_up_at = None
                application.next_action = f"Reply to {mail.from_name or mail.from_addr}"
            add_event(application, "reply_received", sender=mail.from_addr, subject=mail.subject)
            _record(
                session,
                user,
                mail,
                "reply",
                application_id=application.id,
                job_id=application.job_id,
            )
            stats.replies += 1
            return

    if mail.is_bounce or mail.auto_submitted or not looks_like_requirement(mail):
        stats.ignored += 1
        return

    requirement = extract_requirement(mail)
    source = special_source(session, SourceKind.email.value)
    comp = requirement.comp
    job = Job(
        source_id=source.id,
        external_id=sha256_text(mail.message_id)[:40],
        company_name=requirement.vendor[:200] or "Unknown vendor",
        company_key=company_key(requirement.vendor)[:200],
        title=requirement.title[:500],
        fingerprint=job_fingerprint(requirement.client or requirement.vendor, requirement.title),
        location=requirement.location[:500],
        remote=requirement.remote,
        employment_type=requirement.employment_type,
        description_text=requirement.description,
        url="",
        # The Date header is the sender's claim: never let it run ahead of the clock.
        posted_at=min(mail.date, now) if mail.date else now,
        first_seen_at=now,
        last_seen_at=now,
        comp_min=comp.minimum if comp else None,
        comp_max=comp.maximum if comp else None,
        comp_currency=comp.currency if comp else None,
        comp_period=comp.period if comp else None,
        contact_name=requirement.contact_name[:200],
        contact_email=requirement.contact_email[:320],
        client_name=requirement.client[:200],
        facts=extract_facts(
            requirement.description, remote=requirement.remote, location=requirement.location
        ),
        raw={
            "vendor": True,
            "subject": mail.subject,
            "message_id": mail.message_id,
            "references": squash(" ".join([*mail.references, mail.message_id])),
            "duration": requirement.duration,
            # Shown as warnings when the reply is prepared; see apply.service.
            "sender_check_failed": mail.auth_failed,
            "reply_to": mail.reply_to,
        },
    )
    job.workplace = job.facts.get("workplace")
    session.add(job)
    session.flush()
    _record(session, user, mail, "requirement", job_id=job.id)
    stats.requirements += 1


def ingest_inbox(
    session: Session,
    settings: Settings,
    config: UserConfig,
    user: User,
    *,
    fetch: Fetcher = fetch_new,
    now: datetime | None = None,
) -> InboxStats:
    """Read new mail once. Remembers where it got to, so nothing is read twice.

    Each message is committed on its own, so one malformed message can neither
    lose the others nor block the mailbox.
    """
    now = now or utcnow()
    source = special_source(session, SourceKind.email.value)
    state = dict(source.config or {})
    fetched = fetch(
        settings, uidvalidity=state.get("uidvalidity"), last_uid=state.get("last_uid"), now=now
    )
    stats = InboxStats()
    last_uid = state.get("last_uid") if fetched.uidvalidity == state.get("uidvalidity") else None

    def remember(uid: int | None) -> None:
        source.config = {**state, "uidvalidity": fetched.uidvalidity, "last_uid": uid}
        source.last_crawled_at = now
        source.last_ok_at = now
        source.last_status = "ok"
        source.last_error = None
        session.commit()

    own = [settings.mail_from or ""]
    for uid, raw in fetched.messages:
        stats.read += 1
        last_uid = max(last_uid or 0, uid)
        # The position is saved before the message is looked at, so a message
        # that crashes or hangs the process is not read again on restart.
        remember(last_uid)
        try:
            if raw:
                process_message(
                    session, config, user, parse_message(raw), stats, now, own_addresses=own
                )
                session.commit()
            else:
                stats.ignored += 1
        except Exception:
            session.rollback()
            log.exception("could not process message uid %s", uid)
            stats.ignored += 1
    remember(last_uid)
    return stats
