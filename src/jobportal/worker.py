"""The background worker.

Two cadences in one loop: every few seconds it prepares what you asked for and
sends what you approved; every ``crawl_minutes`` it re-reads the sources (and
your mailbox, when configured) and scores what is new.

Your YAML files are re-read on every pass, so an edit takes effect without a
restart, and a broken edit pauses sending instead of acting on stale rules.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from jobportal.apply.service import recover_interrupted
from jobportal.browser import LazyBrowser
from jobportal.config import ConfigError, load_user_config
from jobportal.db import get_session_factory, utcnow
from jobportal.http import PoliteClient
from jobportal.inbox.imap import InboxError
from jobportal.inbox.ingest import ingest_inbox
from jobportal.llm import LLM
from jobportal.pipeline import (
    RunSummary,
    crawl_and_score,
    make_transport,
    prepare_pending,
    send_approved,
)
from jobportal.settings import Settings, get_settings
from jobportal.users import get_default_user

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        crawl_minutes: int = 60,
        tick_seconds: float = 15.0,
        browser_factory: Callable[[], LazyBrowser] | None = None,
        client_factory: Callable[[], PoliteClient] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._browser_factory = browser_factory or (lambda: LazyBrowser(self.settings))
        self._client_factory = client_factory or (lambda: PoliteClient(self.settings))
        self.crawl_every = timedelta(minutes=crawl_minutes)
        self.tick_seconds = tick_seconds
        self.stop_event = threading.Event()
        self.last_crawl: datetime | None = None
        self.last_error: str = ""
        self.last_summary: list[str] = []

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        log.info("worker started (crawl every %s, tick %ss)", self.crawl_every, self.tick_seconds)
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(self.tick_seconds)
        log.info("worker stopped")

    def tick(self, now: datetime | None = None) -> RunSummary | None:
        """One pass. Never raises: a failure is logged and the loop carries on."""
        now = now or utcnow()
        try:
            config = load_user_config(self.settings.data_dir)
        except ConfigError as exc:
            if str(exc) != self.last_error:
                log.error("configuration problem; nothing will be sent until it is fixed:\n%s", exc)
            self.last_error = str(exc)
            return None
        self.last_error = ""

        summary = RunSummary()
        session = get_session_factory()()
        try:
            with self._client_factory() as client, self._browser_factory() as browser:
                recover_interrupted(session, now=now)
                session.commit()
                due = self.last_crawl is None or now - self.last_crawl >= self.crawl_every
                if due:
                    if self.settings.imap_configured:
                        try:
                            user = get_default_user(session, config.profile)
                            log.info(
                                ingest_inbox(session, self.settings, config, user, now=now).line()
                            )
                        except InboxError as exc:
                            summary.errors.append(f"Mailbox: {exc}")
                    crawl_and_score(
                        session, config, client, summary, now=now, min_interval=self.crawl_every / 2
                    )
                    self.last_crawl = now
                prepare_pending(
                    session, self.settings, config, summary,
                    browser=browser, client=client, llm=LLM(self.settings), now=now,
                )  # fmt: skip
                send_approved(
                    session, self.settings, config, summary,
                    transport=make_transport(self.settings), browser=browser, client=client, now=now,
                )  # fmt: skip
        except Exception as exc:
            session.rollback()
            log.exception("worker pass failed")
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        finally:
            session.close()

        lines = summary.lines()
        if lines != ["Nothing to do."]:
            self.last_summary = lines
            for line in lines:
                log.info(line)
        return summary


def start_in_thread(worker: Worker) -> threading.Thread:
    thread = threading.Thread(target=worker.run_forever, name="jobportal-worker", daemon=True)
    thread.start()
    return thread
