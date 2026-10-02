"""Who may talk to the web app.

The app can send email and submit applications under your name, so it treats
its own front door seriously even on localhost:

* It refuses to listen beyond loopback without a password.
* Requests must arrive under a host name it knows (stops DNS rebinding).
* State-changing requests must come from its own pages (stops another website
  in your browser from posting to ``localhost``).
* With a password set, everything except the login page needs a session, and
  a session ends when you sign out or the password changes.
* Without a password, only this machine itself may connect, however the app
  is being served.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from jobportal.settings import Settings

log = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MIN_PASSWORD_LENGTH = 8
MIN_SECRET_LENGTH = 32
LOGIN_WINDOW_SECONDS = 60.0
LOGIN_MAX_DELAY_SECONDS = 5.0
LOGIN_MAX_WAITING = 20
LOGIN_MAX_ADDRESSES = 1024
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def require_safe_binding(host: str, settings: Settings) -> None:
    """Refuse to expose the app to a network without a password."""
    if not is_loopback(host) and settings.password is None:
        raise RuntimeError(
            f"Refusing to listen on {host} without a password: this app can send email and "
            "submit applications as you. Set JOBPORTAL_PASSWORD, or listen on 127.0.0.1."
        )
    check_secrets(settings)


def check_secrets(settings: Settings) -> None:
    """Refuse to run with a password or signing key too short to be worth having."""
    password = settings.password.get_secret_value() if settings.password else None
    if password is not None and len(password) < MIN_PASSWORD_LENGTH:
        raise RuntimeError(
            f"JOBPORTAL_PASSWORD is shorter than {MIN_PASSWORD_LENGTH} characters. "
            "Choose a longer one (or remove it to use the app on this machine only)."
        )
    key = settings.secret_key.get_secret_value() if settings.secret_key else None
    if key is not None and len(key) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JOBPORTAL_SECRET_KEY is shorter than {MIN_SECRET_LENGTH} characters. Remove it "
            "to have a key generated for you, or set a long random value."
        )


def _write_private(path: Path, value: str) -> None:
    """Write a small secret file that only you can read, replacing any old one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        path.unlink()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)


def session_secret(settings: Settings) -> str:
    """The cookie-signing key: from settings, or generated once into the data folder."""
    if settings.secret_key is not None:
        check_secrets(settings)
        return settings.secret_key.get_secret_value()
    path: Path = settings.data_dir / ".session-key"
    with suppress(OSError):
        stored = path.read_text(encoding="utf-8").strip()
        if len(stored) >= MIN_SECRET_LENGTH:
            return stored
        log.warning("%s was empty or too short; a new signing key was generated", path)
    key = secrets.token_urlsafe(48)
    _write_private(path, key)
    return key


def load_session_epoch(settings: Settings) -> int:
    """A counter that every session carries; raising it ends them all."""
    with suppress(OSError, ValueError):
        return int((settings.data_dir / ".session-epoch").read_text(encoding="utf-8").strip())
    return 0


def end_all_sessions(settings: Settings, current: int) -> int:
    """Signing out ends every session, on every device: returns the new epoch."""
    epoch = current + 1
    try:
        _write_private(settings.data_dir / ".session-epoch", str(epoch))
    except OSError as exc:  # still ends them for as long as this process runs
        log.warning("could not store the session epoch: %s", exc)
    return epoch


def password_fingerprint(settings: Settings, secret: str) -> str:
    """Ties a session to the password it was opened with: change one, lose the other."""
    if settings.password is None:
        return ""
    digest = hmac.new(
        secret.encode(), settings.password.get_secret_value().encode(), hashlib.sha256
    )
    return digest.hexdigest()[:32]


def allowed_hosts(settings: Settings) -> set[str]:
    hosts = {"localhost", "127.0.0.1", "[::1]"}
    hosts.add(settings.host.lower())
    hosts.update(h.lower() for h in settings.allowed_hosts)
    return hosts


def _host_only(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:8000
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


class LoginLimiter:
    """Slows password guessing down without ever locking the owner out.

    Only failures count. Each one makes the next attempt from that address
    wait a little longer (and, when failures arrive from many addresses, every
    attempt), up to a few seconds. A correct password always works; it may
    just have to wait its turn. Behind a proxy or Docker every visitor shares
    one address, which is exactly why this delays rather than refuses.
    """

    def __init__(self) -> None:
        self._everyone: deque[float] = deque()
        self._by_address: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._waiting = 0
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def _prune(self, now: float) -> None:
        horizon = now - LOGIN_WINDOW_SECONDS
        while self._everyone and self._everyone[0] < horizon:
            self._everyone.popleft()
        for address in list(self._by_address):
            attempts = self._by_address[address]
            while attempts and attempts[0] < horizon:
                attempts.popleft()
            if not attempts:
                del self._by_address[address]

    def delay(self, address: str) -> float:
        """How long the next attempt from ``address`` has to wait."""
        with self._lock:
            self._prune(time.monotonic())
            mine = len(self._by_address.get(address, ()))
            steps = max(mine, len(self._everyone) // 4)
        return 0.0 if steps == 0 else min(LOGIN_MAX_DELAY_SECONDS, 0.25 * 2 ** (steps - 1))

    def failed(self, address: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._everyone.append(now)
            if address not in self._by_address and len(self._by_address) >= LOGIN_MAX_ADDRESSES:
                del self._by_address[next(iter(self._by_address))]  # the table stays bounded
            self._by_address.setdefault(address, deque()).append(now)

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._by_address.pop(address, None)

    async def wait(self, address: str) -> bool:
        """Serve the delay. ``False`` when too many attempts are already waiting."""
        delay = self.delay(address)
        if delay <= 0:
            return True
        with self._lock:
            if self._waiting >= LOGIN_MAX_WAITING:
                return False
            self._waiting += 1
        try:
            await self.sleep(delay)
        finally:
            with self._lock:
                self._waiting -= 1
        return True


def password_matches(given: str, settings: Settings) -> bool:
    if settings.password is None or not given:
        return False  # an empty form never signs anyone in
    return hmac.compare_digest(given.encode(), settings.password.get_secret_value().encode())


class GuardMiddleware(BaseHTTPMiddleware):
    """Host check, same-origin check for writes, login requirement, security headers."""

    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.settings = settings
        self.hosts = allowed_hosts(settings)

    def _signed_in(self, request: Request) -> bool:
        session = request.session
        state = request.app.state
        return bool(
            session.get("authenticated")
            and session.get("epoch") == getattr(state, "session_epoch", 0)
            and hmac.compare_digest(
                str(session.get("key", "")), getattr(state, "password_fingerprint", "")
            )
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = _host_only(request.headers.get("host", ""))
        if host not in self.hosts:
            return PlainTextResponse(
                "Unknown host name. Add it to JOBPORTAL_ALLOWED_HOSTS if it is yours.",
                status_code=400,
            )

        if self.settings.password is None:
            # No password: this machine only, whatever server is in front of us.
            client = request.client.host if request.client else ""
            if not is_loopback(client):
                return PlainTextResponse(
                    "This app has no password, so it only answers this machine itself. "
                    "Set JOBPORTAL_PASSWORD to use it from elsewhere.",
                    status_code=403,
                )

        if request.method not in SAFE_METHODS and not self._same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)

        path = request.url.path
        public = path in ("/login", "/healthz") or path.startswith("/static/")
        signed_in = self._signed_in(request)
        if self.settings.password is not None and not public and not signed_in:
            if request.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)

        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @staticmethod
    def _same_origin(request: Request) -> bool:
        site = request.headers.get("sec-fetch-site")
        if site is not None:
            return site in ("same-origin", "none")
        origin = request.headers.get("origin")
        if origin is not None:
            # Scheme aside, host *and port* must be this app's own.
            return urlsplit(origin).netloc.lower() == request.headers.get("host", "").lower()
        # Neither header: not a browser page (curl, scripts). Browsers always send
        # at least one of them on a cross-site write.
        return True
