from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Browser

from jobportal import netguard
from jobportal.apply.answers import AnswerBook
from jobportal.apply.forms.filler import prepare
from jobportal.config import UserConfig
from jobportal.http import AddressRefused, PoliteClient
from jobportal.netguard import UrlRefused, check_public_url, is_local_host
from jobportal.settings import Settings
from tests.conftest import FakeWeb
from tests.formserver import FormServer


@pytest.mark.parametrize(
    "host",
    [
        "localhost", "LOCALHOST", "app.localhost", "printer.local", "db.internal", "nas.lan",
        "127.0.0.1", "127.8.9.1", "10.0.0.5", "192.168.1.1", "172.16.0.9", "169.254.169.254",
        "0.0.0.0", "::1", "[::1]", "fe80::1", "fd00::5", "::ffff:127.0.0.1", "",
    ],
)  # fmt: skip
def test_local_and_private_hosts(host: str) -> None:
    assert is_local_host(host)


@pytest.mark.parametrize(
    "host", ["boards-api.greenhouse.io", "jobs.lever.co", "8.8.8.8", "2606:4700::1111"]
)
def test_public_hosts(host: str) -> None:
    assert not is_local_host(host)


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.1", "100.100.100.200", "192.88.99.1", "198.18.0.1", "fec0::1",
        "64:ff9b::7f00:1", "2002:7f00:1::", "::ffff:10.0.0.1", "224.0.0.1", "not an address", "",
    ],
)  # fmt: skip
def test_addresses_that_are_not_public(address: str) -> None:
    assert not netguard.is_public_address(address)
    if address and " " not in address:
        assert is_local_host(address)


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "93.184.216.34", "2606:4700::1111", "[2606:4700::1111]"]
)
def test_addresses_that_are_public(address: str) -> None:
    assert netguard.is_public_address(address)


def test_lookups_are_never_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([("93.184.216.34",), ("127.0.0.1",)])
    monkeypatch.setattr(netguard, "resolve", lambda _host: next(answers))
    assert not is_local_host("rebind.example")
    assert is_local_host("rebind.example")  # the second answer is looked at, not a cached first


def test_names_that_resolve_to_private_addresses_are_local(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {
        "rebind.example": ("10.1.2.3",),
        "mixed.example": ("93.184.216.34", "127.0.0.1"),
        "ok.example": ("93.184.216.34",),
    }
    monkeypatch.setattr(netguard, "resolve", lambda host: table.get(host, ()))
    assert is_local_host("rebind.example")
    assert is_local_host("mixed.example")  # one bad address is enough
    assert not is_local_host("ok.example")
    assert not is_local_host("does-not-resolve.example")


def test_check_public_url() -> None:
    check_public_url("https://jobs.lever.co/globex/1/apply", require_https=True)
    check_public_url("http://example.com/careers")
    for bad in (
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "not a url",
        "https://",
    ):
        with pytest.raises(UrlRefused, match="not a web address"):
            check_public_url(bad)
    for malformed in (
        "http://[::1",
        "http://example.com:notaport/x",
        "https://exa mple.com:99999/",
    ):
        with pytest.raises(UrlRefused):
            check_public_url(malformed)
    with pytest.raises(UrlRefused, match="local or private"):
        check_public_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(UrlRefused, match="not https"):
        check_public_url("http://example.com/apply", require_https=True)
    check_public_url("http://127.0.0.1:9000/form", allow_local=True, require_https=True)


def test_crawler_refuses_local_addresses(client: PoliteClient, web: FakeWeb) -> None:
    with pytest.raises(AddressRefused, match="local or private"):
        client.get("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(AddressRefused):
        client.post_json("http://localhost:8000/sources", {})
    assert web.requests == []  # nothing was sent, not even a robots.txt probe


def test_crawler_refuses_a_redirect_into_the_local_network(
    client: PoliteClient, web: FakeWeb
) -> None:
    web.add(
        "GET",
        "https://careers.example.com/jobs",
        httpx.Response(302, headers={"location": "http://192.168.1.1/admin"}),
    )
    with pytest.raises(AddressRefused, match="redirected"):
        client.get("https://careers.example.com/jobs")
    assert not any(request.url.host == "192.168.1.1" for request in web.requests)


@pytest.mark.browser
def test_browser_does_not_load_local_resources_for_a_public_page(
    browser: Browser, form_server: FormServer, user_config: UserConfig, settings: Settings, tmp_path
) -> None:
    """With local addresses off, the automated browser will not touch one."""
    settings.allow_local_addresses = False
    book = AnswerBook(user_config.profile, {}, tmp_path / "resume.pdf")
    outcome = prepare(browser, form_server.url("classic.html"), book, settings=settings)
    assert outcome.status == "needs_human" and outcome.blockers[0]["kind"] == "url"
    assert form_server.requests == []
