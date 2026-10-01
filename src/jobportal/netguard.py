"""Keep automated requests on the public internet.

Addresses come from third parties: a job board's API names the application
page, a careers page links to a board. None of them may steer the crawler or
the browser at this machine or the network it sits on (a router, a cloud
metadata service, another local app). Every outgoing URL is checked here,
including each hop of a redirect.
"""

from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlsplit

_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class UrlRefused(ValueError):
    """The URL points somewhere automated requests must not go."""


def _is_internal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


@lru_cache(maxsize=2048)
def resolve(host: str) -> tuple[str, ...]:
    """The addresses a host name resolves to; empty when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return ()
    return tuple({str(info[4][0]) for info in infos})


def is_local_host(host: str) -> bool:
    """True for loopback, private, link-local and similar destinations."""
    host = host.strip().strip("[]").lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        return _is_internal(ipaddress.ip_address(host))
    except ValueError:
        pass
    for resolved in resolve(host):
        try:
            if _is_internal(ipaddress.ip_address(resolved.split("%", 1)[0])):
                return True
        except ValueError:
            continue
    return False


def is_local_url(url: str) -> bool:
    return is_local_host(urlsplit(url).hostname or "")


def check_public_url(url: str, *, allow_local: bool = False, require_https: bool = False) -> None:
    """Raise :class:`UrlRefused` unless ``url`` is an http(s) URL on the public internet."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UrlRefused(f"not a web address: {url!r}")
    if is_local_host(parts.hostname):
        if not allow_local:
            raise UrlRefused(f"refusing to open a local or private address: {parts.hostname}")
        return
    if require_https and parts.scheme != "https":
        raise UrlRefused(f"refusing to open a page that is not https: {url}")
