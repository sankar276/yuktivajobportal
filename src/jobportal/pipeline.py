"""The pipeline: crawl -> score -> prepare -> send.

Three steps that can run on their own cadence (the worker crawls every so
often but picks up your approvals within seconds), or all at once with
:func:`run_once`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from jobportal.apply.mail import MailTransport, SmtpTransport
from jobportal.apply.service import (
    prepare_application,
    recover_interrupted,
    submit_application,
)
from jobportal.browser import LazyBrowser
from jobportal.config import UserConfig
from jobportal.crawl import CrawlResult, crawl
from jobportal.db import utcnow
from jobportal.http import PoliteClient
from jobportal.llm import LLM
from jobportal.models import Application, AppStatus, Decision, Job, JobScore
from jobportal.scoring import ScoreStats, score_jobs, search_terms, title_matches_any_lane
from jobportal.settings import Settings
from jobportal.sources import CrawlContext
from jobportal.users import get_default_user

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    crawled: list[CrawlResult] = field(default_factory=list)
    scores: ScoreStats = field(default_factory=ScoreStats)
    prepared: dict[str, int] = field(default_factory=dict)
    sent: int = 0
    deferred: int = 0
    not_sent: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        """A plain-language account of the run."""
        out: list[str] = []
        if self.crawled:
            ok = [r for r in self.crawled if r.status in ("ok", "unchanged")]
            new = sum(r.new for r in self.crawled)
            closed = sum(r.closed for r in self.crawled)
            out.append(
                f"Read {len(ok)} of {len(self.crawled)} sources: {new} new postings, {closed} closed."
            )
            for result in self.crawled:
                if result.status not in ("ok", "unchanged"):
                    out.append(f"  {result.label}: {result.status} ({result.error})")
        s = self.scores
        if s.scored:
            out.append(
                f"Scored {s.scored}: {s.shortlisted} shortlisted, {s.considered} to consider, {s.skipped} skipped."
            )
        if self.prepared:
            parts = ", ".join(
                f"{count} {status.replace('_', ' ')}"
                for status, count in sorted(self.prepared.items())
            )
            out.append(f"Prepared applications: {parts}.")
        if self.sent or self.deferred or self.not_sent:
            line = f"Sent {self.sent}."
            if self.deferred:
                line += f" {self.deferred} waiting for the mail throttle."
            for status, count in sorted(self.not_sent.items()):
                line += f" {count} {status.replace('_', ' ')}."
            out.append(line)
        out.extend(f"Error: {error}" for error in self.errors)
        return out or ["Nothing to do."]


def make_transport(settings: Settings) -> MailTransport | None:
    return SmtpTransport(settings) if settings.smtp_configured else None


def crawl_and_score(
    session: Session,
    config: UserConfig,
    client: PoliteClient,
    summary: RunSummary,
    *,
    now: datetime,
    min_interval: timedelta | None = None,
    do_crawl: bool = True,
) -> None:
    user = get_default_user(session, config.profile)
    search = config.search
    if do_crawl:
        summary.crawled = crawl(
            session,
            client,
            context=CrawlContext(search_terms=search_terms(search)),
            title_filter=partial(title_matches_any_lane, search),
            min_interval=min_interval,
            now=now,
        )
    summary.scores = score_jobs(session, user.id, search, now=now, profile=config.profile)
    session.commit()


def prepare_pending(
    session: Session,
    settings: Settings,
    config: UserConfig,
    summary: RunSummary,
    *,
    browser: LazyBrowser,
    client: PoliteClient | None,
    llm: LLM | None = None,
    now: datetime,
) -> None:
    """Prepare what you asked for, then the best new shortlisted roles."""
    user = get_default_user(session, config.profile)
    requested = session.scalars(
        select(Job)
        .join(Application, Application.job_id == Job.id)
        .where(Application.user_id == user.id, Application.status == AppStatus.preparing.value)
        .order_by(Application.id)
    ).all()
    fresh = session.scalars(
        select(Job)
        .join(JobScore, JobScore.job_id == Job.id)
        .where(
            JobScore.user_id == user.id,
            JobScore.decision == Decision.shortlist.value,
            JobScore.hidden.is_(False),
            Job.closed_at.is_(None),
            ~exists().where(Application.job_id == Job.id, Application.user_id == user.id),
        )
        .order_by(JobScore.score.desc(), Job.id)
        .limit(config.search.policy.prepare_per_run)
    ).all()

    for job in [*requested, *fresh]:
        try:
            application = prepare_application(
                session,
                settings,
                config,
                user,
                job,
                browser=browser,
                client=client,
                llm=llm,
                now=now,
            )
            session.commit()
            summary.prepared[application.status] = summary.prepared.get(application.status, 0) + 1
        except Exception as exc:  # one bad posting must not stop the run
            session.rollback()
            log.exception("preparing job %s failed", job.id)
            summary.errors.append(f"Preparing '{job.title}' at {job.company_name}: {exc}")


def send_approved(
    session: Session,
    settings: Settings,
    config: UserConfig,
    summary: RunSummary,
    *,
    transport: MailTransport | None,
    browser: LazyBrowser,
    client: PoliteClient | None,
    now: datetime,
) -> None:
    user = get_default_user(session, config.profile)
    approved = session.scalars(
        select(Application)
        .where(Application.user_id == user.id, Application.status == AppStatus.approved.value)
        .order_by(Application.approved_at, Application.id)
    ).all()
    mail_paused = False
    for application in approved:
        if mail_paused and application.channel == "email":
            summary.deferred += 1
            continue
        try:
            result = submit_application(
                session, settings, config, application,
                transport=transport, browser=browser, client=client, now=now,
            )  # fmt: skip
        except Exception as exc:
            session.rollback()
            log.exception("sending application %s failed", application.id)
            summary.errors.append(f"Sending application #{application.id}: {exc}")
            continue
        if result == "deferred":
            summary.deferred += 1
            mail_paused = True  # the throttle applies to every further email this round
        elif result == AppStatus.submitted.value:
            summary.sent += 1
        else:
            summary.not_sent[result] = summary.not_sent.get(result, 0) + 1


def run_once(
    session: Session,
    settings: Settings,
    config: UserConfig,
    *,
    client: PoliteClient,
    browser: LazyBrowser,
    transport: MailTransport | None,
    llm: LLM | None = None,
    now: datetime | None = None,
    do_crawl: bool = True,
    min_interval: timedelta | None = None,
) -> RunSummary:
    now = now or utcnow()
    summary = RunSummary()
    recover_interrupted(session, now=now)
    session.commit()
    crawl_and_score(
        session, config, client, summary, now=now, min_interval=min_interval, do_crawl=do_crawl
    )
    prepare_pending(
        session, settings, config, summary, browser=browser, client=client, llm=llm, now=now
    )
    send_approved(
        session,
        settings,
        config,
        summary,
        transport=transport,
        browser=browser,
        client=client,
        now=now,
    )
    return summary
