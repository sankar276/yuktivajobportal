"""Keep automated requests on the public internet.

Addresses come from third parties: a job board's API names the application
page, a careers page links to a board. None of them may steer the crawler or
the browser at this machine or the network it sits on (a router, a cloud
metadata service, another local app). Every outgoing URL is checked here,
including each hop of a redirect, and the crawler then connects to the very
address that was checked (see :func:`public_addresses`), so a name that
answers differently the second time it is looked up gains nothing.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import SplitResult, urlsplit

_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
#: Not public, although the standard library's ``is_global`` lets some through.
_NOT_PUBLIC = tuple(
    ipaddress.ip_network(network)
    for network in (
        "100.64.0.0/10",  # carrier-grade NAT; also where some clouds put metadata
        "192.88.99.0/24",  # 6to4 relay anycast (deprecated)
        "198.18.0.0/15",  # benchmarking
        "fec0::/10",  # site-local (deprecated)
        "2001::/32",  # Teredo
    )
)
#: IPv6 prefixes that carry an IPv4 address inside them, at this bit offset.
_EMBEDDED_V4 = (
    (ipaddress.ip_network("64:ff9b::/96"), 0),  # NAT64
    (ipaddress.ip_network("2002::/16"), 80),  # 6to4
)


class UrlRefused(ValueError):
    """The URL points somewhere automated requests must not go."""


def is_public_address(address: str) -> bool:
    """Is this IP address an ordinary, publicly routable one?

    The one test used for every outgoing connection. Loopback, private,
    link-local, shared, reserved, multicast and unspecified addresses are not
    public; neither is anything that fails to parse.
    """
    try:
        parsed = ipaddress.ip_address(address.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return is_public_address(str(parsed.ipv4_mapped))
        for network, shift in _EMBEDDED_V4:
            if parsed in network:
                embedded = ipaddress.IPv4Address((int(parsed) >> shift) & 0xFFFFFFFF)
                return is_public_address(str(embedded))
    if any(parsed in network for network in _NOT_PUBLIC if network.version == parsed.version):
        return False
    return parsed.is_global and not parsed.is_multicast


def resolve(host: str) -> tuple[str, ...]:
    """The addresses a host name resolves to *now*; empty when it does not resolve.

    Deliberately not cached: an answer that was fine an hour ago says nothing
    about the address a new connection would use.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return ()
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


def _clean(host: str) -> str:
    return host.strip().strip("[]").lower().rstrip(".")


def _local_by_name(host: str) -> bool:
    return not host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES)


def is_local_host(host: str) -> bool:
    """True for loopback, private, link-local and similar destinations."""
    host = _clean(host)
    if _local_by_name(host):
        return True
    if _is_ip(host):
        return not is_public_address(host)
    return any(not is_public_address(address) for address in resolve(host))


def is_local_url(url: str) -> bool:
    try:
        return is_local_host(urlsplit(url).hostname or "")
    except ValueError:
        return True  # unparseable: never somewhere to send a request


def _split(url: str) -> SplitResult:
    try:
        parts = urlsplit(url)
        _ = parts.port  # a bad port only shows when it is read
    except ValueError as exc:
        raise UrlRefused(f"not a web address: {url!r}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UrlRefused(f"not a web address: {url!r}")
    return parts


def public_addresses(
    url: str, *, allow_local: bool = False, require_https: bool = False
) -> tuple[str, ...]:
    """Check ``url`` and return the addresses it may be reached at.

    The name is looked up once, here. A caller that connects to one of the
    returned addresses (instead of letting its HTTP library look the name up
    again) is connecting to exactly what was checked. Empty when the host is
    a literal address or does not resolve.
    """
    parts = _split(url)
    host = _clean(parts.hostname or "")
    if _is_ip(host):
        addresses: tuple[str, ...] = ()
        local = not is_public_address(host)
    else:
        addresses = () if _local_by_name(host) else resolve(host)
        local = _local_by_name(host) or any(not is_public_address(a) for a in addresses)
    if local:
        if not allow_local:
            raise UrlRefused(f"refusing to open a local or private address: {parts.hostname}")
        return addresses
    if require_https and parts.scheme != "https":
        raise UrlRefused(f"refusing to open a page that is not https: {url}")
    return addresses


def check_public_url(url: str, *, allow_local: bool = False, require_https: bool = False) -> None:
    """Raise :class:`UrlRefused` unless ``url`` is an http(s) URL on the public internet."""
    public_addresses(url, allow_local=allow_local, require_https=require_https)
