"""The proxy the unattended browser is made to use, and how Chromium is started."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import httpx
import pytest
from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError

from jobportal import browser as browser_module
from jobportal.apply.forms import filler
from jobportal.browser import BrowserUnavailable, start_chromium
from jobportal.egress import EgressProxy
from jobportal.settings import Settings


class Site:
    """A local web server that records what reaches it and answers as told."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.heads: list[str] = []
        self.pages: dict[str, tuple[int, dict[str, str], str]] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def _answer(self) -> None:
                owner.requests.append((self.command, self.path))
                owner.heads.append(f"{self.requestline}\n{self.headers}")
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                status, headers, body = owner.pages.get(self.path, (200, {}, "ok"))
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def site() -> Iterator[Site]:
    server = Site()
    yield server
    server.close()


@pytest.fixture
def inside() -> Iterator[Site]:
    """Stands in for something on the local network that must never be reached."""
    server = Site()
    yield server
    server.close()


def _ask(proxy: EgressProxy, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", int(proxy.url.rsplit(":", 1)[1])), 5) as conn:
        conn.sendall(request)
        conn.settimeout(5)
        received = b""
        while chunk := conn.recv(65536):
            received += chunk
        return received


# ------------------------------------------------------------------- refusing


@pytest.mark.parametrize(
    "target",
    ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}", "169.254.169.254:80", "10.0.0.5:443"],
)
def test_connect_to_a_private_address_is_refused(site: Site, target: str) -> None:
    target = target.format(port=site.port)
    with EgressProxy() as proxy:
        answer = _ask(proxy, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        assert answer.startswith(b"HTTP/1.1 403 ")
        assert len(proxy.refused) == 1
    assert site.requests == []


def test_plain_http_to_a_private_address_is_refused(site: Site) -> None:
    with EgressProxy() as proxy:
        request = f"GET {site.url('/admin')} HTTP/1.1\r\nHost: 127.0.0.1:{site.port}\r\n\r\n"
        assert _ask(proxy, request.encode()).startswith(b"HTTP/1.1 403 ")
    assert site.requests == []


def test_a_name_that_also_points_inward_is_refused(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One public answer and one private one: the browser could be handed either.
    monkeypatch.setattr("jobportal.netguard.resolve", lambda _host: ("93.184.216.34", "127.0.0.1"))
    with EgressProxy() as proxy:
        request = f"CONNECT jobs.example:{site.port} HTTP/1.1\r\n\r\n".encode()
        assert _ask(proxy, request).startswith(b"HTTP/1.1 403 ")
        assert list(proxy.refused) == [f"jobs.example:{site.port}"]


def test_a_name_that_does_not_resolve_is_refused() -> None:
    with EgressProxy() as proxy:  # tests have no DNS: nothing resolves
        assert _ask(proxy, b"CONNECT nowhere.example:443 HTTP/1.1\r\n\r\n").startswith(
            b"HTTP/1.1 403 "
        )


@pytest.mark.parametrize(
    "request_bytes",
    [
        b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",  # not a proxy request
        b"GET ftp://example.com/file HTTP/1.1\r\n\r\n",
        b"CONNECT example.com HTTP/1.1\r\n\r\n",  # no port
        b"CONNECT example.com:https HTTP/1.1\r\n\r\n",
        b"nonsense\r\n\r\n",
    ],
)
def test_anything_but_a_proxy_request_is_turned_away(request_bytes: bytes) -> None:
    with EgressProxy() as proxy:
        assert _ask(proxy, request_bytes).startswith(b"HTTP/1.1 400 ")


# ------------------------------------------------------------------- relaying


def test_plain_http_is_relayed_as_an_ordinary_request(site: Site) -> None:
    site.pages["/jobs?team=platform"] = (200, {}, "the listing")
    with (
        EgressProxy(allow=lambda _address, port: port == site.port) as proxy,
        httpx.Client(proxy=proxy.url, trust_env=False) as client,
    ):
        response = client.post(site.url("/jobs?team=platform"), content=b"x" * 100_000)
        assert (response.status_code, response.text) == (200, "the listing")
    assert site.requests == [("POST", "/jobs?team=platform")]  # origin form, body delivered
    head = site.heads[0].lower()
    assert "proxy-connection" not in head and "connection: close" in head


def test_connect_tunnels_bytes_untouched(site: Site) -> None:
    site.pages["/through"] = (200, {}, "tunnelled")
    with EgressProxy(allow=lambda _address, port: port == site.port) as proxy:
        answer = _ask(
            proxy,
            f"CONNECT 127.0.0.1:{site.port} HTTP/1.1\r\n\r\n".encode()
            + b"GET /through HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
        )
    assert answer.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert answer.endswith(b"tunnelled")
    assert site.requests == [("GET", "/through")]


def test_the_connection_goes_to_the_address_that_was_checked(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is looked up once, by the proxy; nothing is looked up again later."""
    lookups: list[str] = []

    def resolve(host: str) -> tuple[str, ...]:
        lookups.append(host)
        return ("127.0.0.1",)

    monkeypatch.setattr("jobportal.netguard.resolve", resolve)
    with (
        EgressProxy(allow=lambda address, _port: address == "127.0.0.1") as proxy,
        httpx.Client(proxy=proxy.url, trust_env=False) as client,
    ):
        assert client.get(f"http://careers.example:{site.port}/").status_code == 200
    assert lookups == ["careers.example"]
    assert site.requests == [("GET", "/")]


# ------------------------------------------------------------------ in Chromium


@pytest.mark.browser
def test_nothing_a_page_does_reaches_an_address_the_proxy_refuses(
    browser: Browser, site: Site, inside: Site
) -> None:
    """Redirect hops, sockets, popups and background requests all go through the proxy."""
    secret = inside.url("/secret")
    site.pages["/image"] = (302, {"Location": secret}, "")
    site.pages["/post"] = (307, {"Location": secret}, "")
    site.pages["/moved"] = (302, {"Location": secret}, "")
    site.pages["/apply"] = (
        200,
        {},
        f"""<!doctype html><title>Apply</title><img src="/image"><h1>Apply here</h1>
        <script>
          fetch("/post", {{method: "POST", body: "x"}}).catch(() => {{}});
          fetch("{secret}", {{mode: "no-cors"}}).catch(() => {{}});
          try {{ new WebSocket("ws://127.0.0.1:{inside.port}/socket"); }} catch (error) {{}}
          window.open("{secret}");
        </script>""",
    )
    with EgressProxy(allow=lambda _address, port: port == site.port) as proxy:
        context = browser.new_context(proxy={"server": proxy.url, "bypass": "<-loopback>"})
        try:
            page = context.new_page()
            page.goto(site.url("/apply"), wait_until="networkidle")
            assert page.inner_text("h1") == "Apply here"  # the public page itself loads
            page.wait_for_timeout(500)
            response = page.goto(site.url("/moved"))  # a navigation redirected inward
            assert response is not None and response.status == 403
        finally:
            context.close()
        assert any(entry.endswith(f":{inside.port}") for entry in proxy.refused)
    assert ("GET", "/apply") in site.requests
    assert inside.requests == []


def test_form_pages_are_opened_through_the_proxy(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(filler, "behind_proxy", lambda: False)
    chromium = MagicMock()
    filler._new_page(chromium, settings)
    proxy = chromium.new_context.call_args.kwargs["proxy"]
    assert proxy["server"].startswith("http://127.0.0.1:") and proxy["bypass"] == "<-loopback>"
    context = chromium.new_context.return_value
    assert context.route.called and context.route_web_socket.called

    # Behind an outbound proxy of the user's own, that proxy does the connecting.
    monkeypatch.setattr(filler, "behind_proxy", lambda: True)
    chromium = MagicMock()
    filler._new_page(chromium, settings)
    assert chromium.new_context.call_args.kwargs == {}
    assert chromium.new_context.return_value.route.called  # the request guard still applies

    # Tests and local demos that allow local addresses are not guarded at all.
    settings.allow_local_addresses = True
    chromium = MagicMock()
    filler._new_page(chromium, settings)
    assert chromium.new_context.call_args.kwargs == {}
    assert not chromium.new_context.return_value.route.called


# ---------------------------------------------------------------------- sandbox


class FakePlaywright:
    """Chromium whose sandbox does or does not start."""

    def __init__(self, *, sandbox_works: bool, installed: bool = True) -> None:
        self.sandbox_works, self.installed = sandbox_works, installed
        self.launches: list[bool] = []
        self.chromium = self

    def launch(self, *, chromium_sandbox: bool, args: list[str], **_options: object) -> str:
        self.launches.append(chromium_sandbox)
        assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
        if not self.installed or (chromium_sandbox and not self.sandbox_works):
            raise PlaywrightError("BrowserType.launch: it did not start\nmore detail")
        return "a browser"


@pytest.fixture
def not_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_module, "_is_root", lambda: False)
    monkeypatch.setattr(browser_module, "_sandbox_unusable", False)


def test_the_sandbox_is_used_where_it_starts(settings: Settings, not_root: None) -> None:
    playwright = FakePlaywright(sandbox_works=True)
    assert start_chromium(playwright, settings) == "a browser"
    assert playwright.launches == [True]


def test_without_a_usable_sandbox_chromium_still_starts_and_says_so(
    settings: Settings, not_root: None, caplog: pytest.LogCaptureFixture
) -> None:
    playwright = FakePlaywright(sandbox_works=False)
    with caplog.at_level("WARNING", logger="jobportal.browser"):
        assert start_chromium(playwright, settings) == "a browser"
        assert start_chromium(playwright, settings) == "a browser"
    assert playwright.launches == [True, False, False]  # the failed start is not repeated
    assert [r.message for r in caplog.records if "sandbox cannot start" in r.message] != []
    assert sum("sandbox cannot start" in record.message for record in caplog.records) == 1


def test_as_root_the_sandbox_is_not_attempted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_module, "_is_root", lambda: True)
    monkeypatch.setattr(browser_module, "_sandbox_unusable", False)
    playwright = FakePlaywright(sandbox_works=False)
    start_chromium(playwright, settings)
    assert playwright.launches == [False]


def test_a_required_sandbox_never_falls_back(settings: Settings, not_root: None) -> None:
    settings.chromium_sandbox = True
    playwright = FakePlaywright(sandbox_works=False)
    with pytest.raises(BrowserUnavailable, match="sandbox is required"):
        start_chromium(playwright, settings)
    assert playwright.launches == [True]
    settings.chromium_sandbox = False
    start_chromium(playwright, settings)
    assert playwright.launches == [True, False]


def test_a_missing_browser_is_reported_as_such(settings: Settings, not_root: None) -> None:
    playwright = FakePlaywright(sandbox_works=True, installed=False)
    with pytest.raises(BrowserUnavailable, match="playwright install chromium"):
        start_chromium(playwright, settings)
    assert playwright.launches == [True, False]


@pytest.mark.parametrize(
    ("written", "value"), [("auto", "auto"), ("true", True), ("false", False), ("1", True)]
)
def test_sandbox_setting_reads_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, written: str, value: object
) -> None:
    monkeypatch.setenv("JOBPORTAL_CHROMIUM_SANDBOX", written)
    assert Settings().chromium_sandbox == value
    assert Settings.model_fields["chromium_sandbox"].default == "auto"
