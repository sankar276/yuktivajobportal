"""The one way out for the unattended browser.

A page the browser opens can start requests that no page-level check ever
sees (each hop of a redirect, a popup, a worker's fetch), and Chromium looks
names up on its own, so a name can give a public address when it is checked
and a private one when it is connected to. So the browser is handed this
proxy and nothing else. Each connection it asks for is looked up once, here;
every address the name gives must be public; and the connection is then made
to that very address.

Nothing is read or changed on the way: TLS passes through untouched
(``CONNECT``), and plain HTTP is relayed one request per connection.
"""

from __future__ import annotations

import logging
import selectors
import socket
import socketserver
import threading
from collections import deque
from collections.abc import Callable
from urllib.parse import urlsplit

from jobportal import netguard

log = logging.getLogger(__name__)

MAX_HEAD_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 15.0
IDLE_TIMEOUT_SECONDS = 120.0
#: Headers that are about the hop to this proxy, not about the request.
_HOP_HEADERS = frozenset({"proxy-connection", "proxy-authorization", "connection", "keep-alive"})

Allow = Callable[[str, int], bool]


def _public_only(address: str, _port: int) -> bool:
    return netguard.is_public_address(address)


class EgressProxy:
    """A forward proxy on this machine that only connects to public addresses."""

    def __init__(self, *, allow: Allow = _public_only) -> None:
        self._allow = allow
        self._server: _Server | None = None
        #: The last destinations that were refused, newest last. For the log and for tests.
        self.refused: deque[str] = deque(maxlen=100)

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the egress proxy is not running")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> EgressProxy:
        if self._server is None:
            server = _Server(("127.0.0.1", 0), _Handler)
            server.proxy = self
            threading.Thread(target=server.serve_forever, name="egress-proxy", daemon=True).start()
            self._server = server
        return self

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def __enter__(self) -> EgressProxy:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def dial(self, host: str, port: int) -> socket.socket | None:
        """A connection to ``host``, at an address that was checked; ``None`` when refused."""
        name = host.strip().strip("[]").rstrip(".").lower()
        if netguard.is_ip(name):
            addresses: tuple[str, ...] = (name,)
        else:
            addresses = netguard.resolve(name)
        # Every answer must be acceptable: a name that also points inward is refused.
        if not addresses or not all(self._allow(address, port) for address in addresses):
            self.refused.append(f"{name}:{port}")
            log.warning("browser request to %s:%s refused: not a public address", name, port)
            return None
        for address in addresses:
            try:
                upstream = socket.create_connection((address, port), CONNECT_TIMEOUT_SECONDS)
            except OSError:
                continue
            upstream.settimeout(IDLE_TIMEOUT_SECONDS)
            return upstream
        return None


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128
    proxy: EgressProxy


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(IDLE_TIMEOUT_SECONDS)
        try:
            head, rest = _read_head(client)
            request_line, *headers = head.decode("latin-1").split("\r\n")
            method, target, version = request_line.split(" ", 2)
            if method.upper() == "CONNECT":
                host, port = _host_port(target)
                path = ""
            else:
                parts = urlsplit(target)
                if parts.scheme.lower() != "http" or not parts.hostname:
                    raise ValueError("only http:// and CONNECT are relayed")
                host, port = parts.hostname, parts.port or 80
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        except (OSError, ValueError):
            _reply(client, 400, "Bad Request")
            return

        upstream = self.server.proxy.dial(host, port)
        if upstream is None:
            _reply(client, 403, "Forbidden")
            return
        with upstream:
            try:
                if method.upper() == "CONNECT":
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    upstream.sendall(rest)
                else:
                    kept = [
                        line
                        for line in headers
                        if line and line.split(":", 1)[0].strip().lower() not in _HOP_HEADERS
                    ]
                    # One request per connection, so the next one is checked afresh.
                    lines = [f"{method} {path} {version}", *kept, "Connection: close", "", ""]
                    upstream.sendall("\r\n".join(lines).encode("latin-1") + rest)
                _relay(client, upstream)
            except OSError:
                return


def _read_head(client: socket.socket) -> tuple[bytes, bytes]:
    """The request line and headers, and whatever arrived after them."""
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEAD_BYTES:
            raise ValueError("request head too large")
        chunk = client.recv(8192)
        if not chunk:
            raise ValueError("connection closed before a request arrived")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _host_port(target: str) -> tuple[str, int]:
    """``host:port`` of a CONNECT request; the host may be a bracketed IPv6 address."""
    host, separator, port = target.rpartition(":")
    if not separator or not host:
        raise ValueError(f"not host:port: {target!r}")
    return host.strip("[]"), int(port)


def _reply(client: socket.socket, status: int, reason: str) -> None:
    body = f"{status} {reason}\n".encode()
    head = (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    try:
        client.sendall(head.encode() + body)
    except OSError:
        return


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    """Copy bytes both ways until either side closes or nothing moves for a while."""
    other = {client: upstream, upstream: client}
    with selectors.DefaultSelector() as selector:
        selector.register(client, selectors.EVENT_READ)
        selector.register(upstream, selectors.EVENT_READ)
        while True:
            ready = selector.select(IDLE_TIMEOUT_SECONDS)
            if not ready:
                return
            for key, _events in ready:
                source = key.fileobj
                assert isinstance(source, socket.socket)
                data = source.recv(65536)
                if not data:
                    return
                other[source].sendall(data)


_shared: EgressProxy | None = None
_shared_lock = threading.Lock()


def shared() -> EgressProxy:
    """This process's proxy, started on first use and left running until exit."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = EgressProxy().start()
        return _shared
