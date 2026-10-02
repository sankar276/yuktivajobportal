from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, sync_playwright
from sqlalchemy.orm import Session

from jobportal.config import UserConfig, load_user_config
from jobportal.db import get_engine, get_session_factory, reset_engine
from jobportal.http import PoliteClient
from jobportal.models import Base, User
from jobportal.settings import Settings, get_settings, reset_settings_cache
from jobportal.users import get_default_user
from tests.formserver import FormServer

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CONFIG = ROOT / "config"
NOW = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)

_SCRUBBED_ENV = [
    "JOBPORTAL_PASSWORD", "JOBPORTAL_SECRET_KEY", "JOBPORTAL_SMTP_HOST", "JOBPORTAL_SMTP_USERNAME",
    "JOBPORTAL_SMTP_PASSWORD", "JOBPORTAL_IMAP_HOST", "JOBPORTAL_IMAP_USERNAME",
    "JOBPORTAL_IMAP_PASSWORD", "JOBPORTAL_MAIL_FROM", "JOBPORTAL_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY", "JOBPORTAL_HOST", "JOBPORTAL_ALLOWED_HOSTS",
]  # fmt: skip


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets its own data directory, database and settings."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("JOBPORTAL_DATA_DIR", str(data_dir))
    url = os.environ.get("JOBPORTAL_TEST_DATABASE_URL") or f"sqlite:///{data_dir}/test.db"
    monkeypatch.setenv("JOBPORTAL_DATABASE_URL", url)
    monkeypatch.setenv("JOBPORTAL_PER_HOST_DELAY_SECONDS", "0")
    for name in _SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # so a developer's .env is never read
    # No DNS from tests: unknown names simply do not resolve.
    monkeypatch.setattr("jobportal.netguard.resolve", lambda _host: ())
    reset_settings_cache()
    reset_engine()
    if os.environ.get("JOBPORTAL_TEST_DATABASE_URL"):
        _empty_shared_database()
    yield
    reset_engine()
    reset_settings_cache()


def _empty_shared_database() -> None:
    """A server database outlives each test: start every one with nothing in it.

    SQLite gets a new file per test. A Postgres database is shared, so what
    one test stored (through the command line, say, which keeps its own
    session) must not be there for the next.
    """
    engine = get_engine()
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")


@pytest.fixture
def settings() -> Settings:
    settings = get_settings()
    settings.ensure_dirs()
    return settings


@pytest.fixture
def data_dir(settings: Settings) -> Path:
    """A data directory seeded with the example configuration."""
    for name in ("profile", "search", "resume"):
        shutil.copy(CONFIG / f"{name}.example.yaml", settings.data_dir / f"{name}.yaml")
    return settings.data_dir


@pytest.fixture
def user_config(data_dir: Path) -> UserConfig:
    return load_user_config(data_dir)


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def user(session: Session, user_config: UserConfig) -> User:
    user = get_default_user(session, user_config.profile)
    session.commit()
    return user


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


Handler = Callable[[httpx.Request], httpx.Response]


class FakeWeb:
    """Routes for an ``httpx.MockTransport``; records every request made."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler | httpx.Response] = {}
        self.requests: list[httpx.Request] = []
        self.robots: dict[str, httpx.Response] = {}

    def add(self, method: str, url: str, response: Handler | httpx.Response) -> None:
        self.routes[(method.upper(), url)] = response

    def json(self, method: str, url: str, payload: Any, status: int = 200, **headers: str) -> None:
        self.add(method, url, httpx.Response(status, json=payload, headers=headers))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.url.path == "/robots.txt":
            origin = f"{request.url.scheme}://{request.url.host}"
            return self.robots.get(origin, httpx.Response(404))
        route = self.routes.get((request.method, url))
        if route is None:
            return httpx.Response(404, text=f"no route for {request.method} {url}")
        return route(request) if callable(route) else route

    def calls(self, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if fragment in str(r.url)]


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def client(settings: Settings, web: FakeWeb) -> Iterator[PoliteClient]:
    client = PoliteClient(
        settings, transport=httpx.MockTransport(web.handler), sleep=lambda _s: None
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    """One Chromium for the whole run.

    Playwright's sync API cannot be started twice on one thread, so every test
    that needs a browser shares this one and passes it in explicitly.
    """
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("JOBPORTAL_CHROMIUM_PATH") or None
        )
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def form_server(settings: Settings) -> Iterator[FormServer]:
    """A local stand-in for an ATS; also lets the filler open local addresses."""
    settings.allow_local_addresses = True
    server = FormServer()
    try:
        yield server
    finally:
        server.close()


class CapturedMail:
    """Everything a local SMTP server received."""

    def __init__(self) -> None:
        self.envelopes: list[Any] = []
        self.fail_with: str | None = None

    async def handle_DATA(self, _server: Any, _session: Any, envelope: Any) -> str:
        if self.fail_with:
            return self.fail_with
        self.envelopes.append(envelope)
        return "250 Message accepted for delivery"

    def messages(self) -> list[Any]:
        from email import message_from_bytes
        from email.policy import default

        return [message_from_bytes(e.content, policy=default) for e in self.envelopes]


@pytest.fixture
def smtp_server(settings: Settings) -> Iterator[CapturedMail]:
    """A local mail server; the app is pointed at it for the test."""
    import socket

    from aiosmtpd.controller import Controller

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    captured = CapturedMail()
    controller = Controller(captured, hostname="127.0.0.1", port=port)
    controller.start()
    settings.smtp_host = "127.0.0.1"
    settings.smtp_port = port
    settings.smtp_security = "none"
    try:
        yield captured
    finally:
        controller.stop()
