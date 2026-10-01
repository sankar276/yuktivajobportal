"""A polite HTTP client for reading public job postings.

Every request: identifies itself with a descriptive User-Agent, is checked
against the host's robots.txt (RFC 9309), is spaced out per host, and backs
off on 429/5xx honouring ``Retry-After``. There is deliberately no way to turn
the robots check off.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobportal.db import utcnow
from jobportal.robots import RobotsRules
from jobportal.settings import Settings, get_settings

log = logging.getLogger(__name__)

ROBOTS_TTL_SECONDS = 24 * 3600
ROBOTS_UNREACHABLE_TTL_SECONDS = 15 * 60
MAX_RETRY_AFTER_SECONDS = 120.0


class FetchError(Exception):
    """A request failed after retries."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RobotsDisallowed(FetchError):
    """The host's robots.txt does not allow this path for our crawler."""


class NotFound(FetchError):
    """404/410: the board or posting does not exist (any more)."""


@dataclass
class Response:
    status: int
    url: str
    text: str
    headers: httpx.Headers
    not_modified: bool = False

    def json(self) -> Any:
        import json

        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise FetchError(f"{self.url} did not return JSON: {exc}") from exc

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


class PoliteClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.Client(
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
            },
            timeout=self.settings.http_timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self._sleep = sleep
        self._robots: dict[str, tuple[float, RobotsRules]] = {}
        self._robots_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_next: dict[str, float] = {}
        self._guard = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- robots

    def robots_for(self, url: str) -> RobotsRules:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        now = time.monotonic()
        with self._robots_lock:
            cached = self._robots.get(origin)
            if cached and cached[0] > now:
                return cached[1]
        rules, ttl = self._fetch_robots(origin)
        with self._robots_lock:
            self._robots[origin] = (now + ttl, rules)
        return rules

    def _fetch_robots(self, origin: str) -> tuple[RobotsRules, float]:
        url = f"{origin}/robots.txt"
        self._throttle(origin)
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable for %s (%s); treating as disallow", origin, exc)
            return RobotsRules.disallow_all(), ROBOTS_UNREACHABLE_TTL_SECONDS
        if 200 <= response.status_code < 300:
            rules = RobotsRules.parse(response.text, self.settings.robots_token)
            return rules, ROBOTS_TTL_SECONDS
        if 400 <= response.status_code < 500:
            # RFC 9309 2.3.1.3: "unavailable" means there are no restrictions.
            return RobotsRules.allow_all(), ROBOTS_TTL_SECONDS
        # 5xx: "unreachable" means assume complete disallow, and retry soon.
        log.warning(
            "robots.txt for %s returned %s; treating as disallow", origin, response.status_code
        )
        return RobotsRules.disallow_all(), ROBOTS_UNREACHABLE_TTL_SECONDS

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return self.robots_for(url).allowed(path)

    # ----------------------------------------------------------- throttle

    def _throttle(self, origin: str) -> None:
        with self._guard:
            lock = self._host_locks.setdefault(origin, threading.Lock())
        with lock:
            wait = self._host_next.get(origin, 0.0) - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._host_next[origin] = time.monotonic() + self.settings.per_host_delay_seconds

    # ----------------------------------------------------------- requests

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        conditional = dict(headers or {})
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified
        return self._request("GET", url, headers=conditional)

    def post_json(self, url: str, payload: dict[str, Any]) -> Response:
        return self._request(
            "POST", url, json=payload, headers={"Content-Type": "application/json"}
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> Response:
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        attempts = max(1, self.settings.http_max_attempts)
        last_error: str = "no attempt made"
        last_status: int | None = None

        for attempt in range(1, attempts + 1):
            self._throttle(origin)
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error, last_status = f"{type(exc).__name__}: {exc}", None
                delay = _backoff(attempt)
            else:
                status = response.status_code
                if status == 304:
                    return Response(status, str(response.url), "", response.headers, True)
                if 200 <= status < 300:
                    return Response(status, str(response.url), response.text, response.headers)
                if status in (404, 410):
                    raise NotFound(f"{url} returned {status}", status=status)
                last_error, last_status = f"HTTP {status}", status
                if status != 429 and status < 500:
                    break  # a client error will not get better by retrying
                delay = _retry_after(response) or _backoff(attempt)
            if attempt < attempts:
                log.info("retrying %s in %.1fs after %s", url, delay, last_error)
                self._sleep(delay)

        raise FetchError(f"{method} {url} failed: {last_error}", status=last_status)


def _backoff(attempt: int) -> float:
    return float(min(30, 2**attempt))


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - utcnow()).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))
