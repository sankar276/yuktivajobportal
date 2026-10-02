"""A polite HTTP client for reading public job postings.

Every request: identifies itself with a descriptive User-Agent, is checked
against the host's robots.txt (RFC 9309), is spaced out per host, and backs
off on 429/5xx honouring ``Retry-After``. There is deliberately no way to turn
the robots check off.

It is also a careful one, because every address and every byte it handles
comes from somebody else: each hop of a redirect is checked like a first
request (public address, robots.txt), the connection goes to the very address
that was checked, and a response that is too large or too slow is abandoned.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from jobportal import netguard
from jobportal.db import utcnow
from jobportal.netguard import UrlRefused
from jobportal.robots import RobotsRules
from jobportal.settings import Settings, get_settings

log = logging.getLogger(__name__)

ROBOTS_TTL_SECONDS = 24 * 3600
ROBOTS_UNREACHABLE_TTL_SECONDS = 15 * 60
#: RFC 9309 asks crawlers to read at least 500 KiB of a robots.txt; the rest is ignored.
ROBOTS_MAX_BYTES = 512 * 1024
ROBOTS_CACHE_SIZE = 512
MAX_RETRY_AFTER_SECONDS = 120.0
MAX_REDIRECTS = 5
_REDIRECTS = (301, 302, 303, 307, 308)


class FetchError(Exception):
    """A request failed after retries."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RobotsDisallowed(FetchError):
    """The host's robots.txt does not allow this path for our crawler."""


class AddressRefused(FetchError):
    """The URL (or a redirect from it) points at a local or private address."""


class NotFound(FetchError):
    """404/410: the board or posting does not exist (any more)."""


class ResponseTooLarge(FetchError):
    """The response ran past the size limit and was abandoned."""


class ResponseTooSlow(FetchError):
    """The response was still arriving at the deadline and was abandoned."""


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


@dataclass
class _Raw:
    """One hop's answer, body already read within the limits."""

    status: int
    headers: httpx.Headers
    text: str


class PoliteClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
        pin: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # Connect to the address that was checked rather than letting the
        # library look the name up a second time. Not possible through an
        # outbound proxy (the proxy does the looking up), and pointless with
        # a stand-in transport that has no network underneath.
        self._pin = pin if pin is not None else (transport is None and not netguard.behind_proxy())
        self._client = httpx.Client(
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
            },
            timeout=self.settings.http_timeout_seconds,
            # Redirects are followed here, one checked hop at a time.
            follow_redirects=False,
            transport=transport,
            # Pinned connections are per name, so none is kept for reuse by
            # another name that happens to share the address.
            limits=httpx.Limits(max_keepalive_connections=0 if self._pin else 10),
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

    # ------------------------------------------------------------ one hop

    def _target(self, url: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Check ``url`` and say where to connect: ``(url, headers, extensions)``.

        Raises :class:`UrlRefused` for anything that is not a public web
        address. When pinning, the returned URL carries the checked address
        in place of the name; the name still goes in ``Host`` and in the TLS
        handshake, so the right site answers and its certificate is verified.
        """
        addresses = netguard.public_addresses(url, allow_local=self.settings.allow_local_addresses)
        if not self._pin or not addresses:
            return url, {}, {}
        parts = urlsplit(url)
        address = sorted(addresses, key=lambda a: ":" in a)[0]  # IPv4 first
        netloc = f"[{address}]" if ":" in address else address
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        host = parts.netloc.rsplit("@", 1)[-1]
        return pinned, {"Host": host}, {"sni_hostname": parts.hostname}

    def _send(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        truncate: bool = False,
        headers: dict[str, str] | None = None,
        json: Any = None,
    ) -> _Raw:
        """One request, no redirects followed. The body is read within the limits."""
        target, extra, extensions = self._target(url)
        deadline = time.monotonic() + self.settings.http_deadline_seconds
        with self._client.stream(
            method, target, headers={**(headers or {}), **extra}, json=json, extensions=extensions
        ) as response:
            chunks: list[bytes] = []
            size = 0
            if response.status_code not in _REDIRECTS and response.status_code != 304:
                for chunk in response.iter_bytes():
                    size += len(chunk)  # counted after decompression
                    if size > max_bytes:
                        if truncate:
                            chunks.append(chunk[: len(chunk) - (size - max_bytes)])
                            break
                        raise ResponseTooLarge(
                            f"{url} sent more than {max_bytes:,} bytes; gave up reading it"
                        )
                    chunks.append(chunk)
                    if time.monotonic() > deadline:
                        raise ResponseTooSlow(
                            f"{url} was still sending after "
                            f"{self.settings.http_deadline_seconds:.0f}s; gave up reading it"
                        )
            content = b"".join(chunks)
            try:
                text = content.decode(response.charset_encoding or "utf-8", errors="replace")
            except LookupError:
                text = content.decode("utf-8", errors="replace")
            return _Raw(response.status_code, response.headers, text)

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
            if len(self._robots) >= ROBOTS_CACHE_SIZE:
                # Drop what has expired; if that is not enough, the oldest entries.
                for key in [k for k, (expires, _r) in self._robots.items() if expires <= now]:
                    del self._robots[key]
                while len(self._robots) >= ROBOTS_CACHE_SIZE:
                    del self._robots[next(iter(self._robots))]
            self._robots[origin] = (now + ttl, rules)
        return rules

    def _fetch_robots(self, origin: str) -> tuple[RobotsRules, float]:
        url = f"{origin}/robots.txt"
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                self._throttle(origin)
                raw = self._send("GET", url, max_bytes=ROBOTS_MAX_BYTES, truncate=True)
                location = raw.headers.get("location")
                if raw.status not in _REDIRECTS or not location:
                    break
                url = urljoin(url, location)  # each hop is checked again in _send
            else:
                raise FetchError("too many redirects")
        except (httpx.HTTPError, UrlRefused, FetchError) as exc:
            log.warning("robots.txt unreachable for %s (%s); treating as disallow", origin, exc)
            return RobotsRules.disallow_all(), ROBOTS_UNREACHABLE_TTL_SECONDS
        if 200 <= raw.status < 300:
            return RobotsRules.parse(raw.text, self.settings.robots_token), ROBOTS_TTL_SECONDS
        if 400 <= raw.status < 500:
            # RFC 9309 2.3.1.3: "unavailable" means there are no restrictions.
            return RobotsRules.allow_all(), ROBOTS_TTL_SECONDS
        # 5xx: "unreachable" means assume complete disallow, and retry soon.
        log.warning("robots.txt for %s returned %s; treating as disallow", origin, raw.status)
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
        """Fetch ``url``, following redirects one checked hop at a time."""
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                netguard.check_public_url(current, allow_local=self.settings.allow_local_addresses)
            except UrlRefused as exc:
                if hop == 0:
                    raise AddressRefused(str(exc)) from exc
                raise AddressRefused(f"{url} redirected somewhere it must not: {exc}") from exc
            # A redirect lands on a path with rules of its own, possibly on
            # another host: it is asked for permission like any first request.
            if not self.allowed(current):
                raise RobotsDisallowed(f"robots.txt disallows {current}")
            raw = self._attempts(method, current, **kwargs)
            location = raw.headers.get("location")
            if raw.status in _REDIRECTS and location:
                if method != "GET":
                    raise FetchError(
                        f"{method} {current} answered with a redirect", status=raw.status
                    )
                current = urljoin(current, location)
                continue
            if raw.status == 304:
                return Response(raw.status, current, "", raw.headers, True)
            return Response(raw.status, current, raw.text, raw.headers)
        raise FetchError(f"{url} redirected more than {MAX_REDIRECTS} times")

    def _attempts(self, method: str, url: str, **kwargs: Any) -> _Raw:
        """One URL, with retries. Returns a 2xx, a 304 or a redirect; raises otherwise."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        attempts = max(1, self.settings.http_max_attempts)
        last_error: str = "no attempt made"
        last_status: int | None = None

        for attempt in range(1, attempts + 1):
            self._throttle(origin)
            try:
                raw = self._send(
                    method, url, max_bytes=self.settings.http_max_response_bytes, **kwargs
                )
            except UrlRefused as exc:
                raise AddressRefused(str(exc)) from exc
            except httpx.HTTPError as exc:
                last_error, last_status = f"{type(exc).__name__}: {exc}", None
                delay = _backoff(attempt)
            else:
                status = raw.status
                if status == 304 or 200 <= status < 300 or status in _REDIRECTS:
                    return raw
                if status in (404, 410):
                    raise NotFound(f"{url} returned {status}", status=status)
                last_error, last_status = f"HTTP {status}", status
                if status != 429 and status < 500:
                    break  # a client error will not get better by retrying
                delay = _retry_after(raw.headers) or _backoff(attempt)
            if attempt < attempts:
                log.info("retrying %s in %.1fs after %s", url, delay, last_error)
                self._sleep(delay)

        raise FetchError(f"{method} {url} failed: {last_error}", status=last_status)


def _backoff(attempt: int) -> float:
    return float(min(30, 2**attempt))


def _retry_after(headers: httpx.Headers) -> float | None:
    value = headers.get("retry-after")
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
