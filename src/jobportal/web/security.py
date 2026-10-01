"""Who may talk to the web app.

The app can send email and submit applications under your name, so it treats
its own front door seriously even on localhost:

* It refuses to listen beyond loopback without a password.
* Requests must arrive under a host name it knows (stops DNS rebinding).
* State-changing requests must come from its own pages (stops another website
  in your browser from posting to ``localhost``).
* With a password set, everything except the login page needs a session.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from jobportal.settings import Settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60.0
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


def session_secret(settings: Settings) -> str:
    """The cookie-signing key: from settings, or generated once into the data folder."""
    if settings.secret_key is not None:
        return settings.secret_key.get_secret_value()
    path: Path = settings.data_dir / ".session-key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    path.write_text(key, encoding="utf-8")
    path.chmod(0o600)
    return key


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
    """A small brake on password guessing: a few attempts per minute per address."""

    def __init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        attempts = self._attempts[address]
        while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= LOGIN_ATTEMPTS:
            return False
        attempts.append(now)
        return True


def password_matches(given: str, settings: Settings) -> bool:
    if settings.password is None:
        return False
    return hmac.compare_digest(given.encode(), settings.password.get_secret_value().encode())


class GuardMiddleware(BaseHTTPMiddleware):
    """Host check, same-origin check for writes, login requirement, security headers."""

    def __init__(self, app: object, settings: Settings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.settings = settings
        self.hosts = allowed_hosts(settings)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = _host_only(request.headers.get("host", ""))
        if host not in self.hosts:
            return PlainTextResponse(
                "Unknown host name. Add it to JOBPORTAL_ALLOWED_HOSTS if it is yours.",
                status_code=400,
            )

        if request.method not in SAFE_METHODS and not self._same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)

        path = request.url.path
        public = path in ("/login", "/healthz") or path.startswith("/static/")
        signed_in = bool(request.session.get("authenticated"))
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
