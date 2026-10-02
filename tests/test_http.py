from __future__ import annotations

import httpx
import pytest

from jobportal.http import (
    FetchError,
    NotFound,
    PoliteClient,
    ResponseTooLarge,
    ResponseTooSlow,
    RobotsDisallowed,
)
from jobportal.netguard import UrlRefused
from tests.conftest import FakeWeb

URL = "https://boards.example.com/v1/jobs"
ORIGIN = "https://boards.example.com"


def test_get_returns_json_and_identifies_itself(client: PoliteClient, web: FakeWeb) -> None:
    web.json("GET", URL, {"jobs": []}, etag='"abc"')
    response = client.get(URL)
    assert response.json() == {"jobs": []}
    assert response.etag == '"abc"'
    sent = web.calls("/v1/jobs")[0]
    assert sent.headers["user-agent"].startswith("YuktivaJobPortal/")
    assert "github.com" in sent.headers["user-agent"]


def test_robots_txt_is_checked_once_per_host(client: PoliteClient, web: FakeWeb) -> None:
    web.json("GET", URL, {})
    client.get(URL)
    client.get(URL)
    assert len(web.calls("/robots.txt")) == 1


def test_disallowed_path_is_never_requested(client: PoliteClient, web: FakeWeb) -> None:
    web.robots[ORIGIN] = httpx.Response(200, text="User-agent: *\nDisallow: /v1/\n")
    web.json("GET", URL, {})
    with pytest.raises(RobotsDisallowed):
        client.get(URL)
    assert web.calls("/v1/jobs") == []


def test_robots_applies_to_posts_too(client: PoliteClient, web: FakeWeb) -> None:
    web.robots[ORIGIN] = httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    with pytest.raises(RobotsDisallowed):
        client.post_json(URL, {"q": 1})


def test_robots_server_error_means_disallow(client: PoliteClient, web: FakeWeb) -> None:
    web.robots[ORIGIN] = httpx.Response(503)
    web.json("GET", URL, {})
    with pytest.raises(RobotsDisallowed):
        client.get(URL)


def test_missing_robots_txt_means_allowed(client: PoliteClient, web: FakeWeb) -> None:
    web.robots[ORIGIN] = httpx.Response(404)
    web.json("GET", URL, {"ok": True})
    assert client.get(URL).json() == {"ok": True}


def test_conditional_request_and_not_modified(client: PoliteClient, web: FakeWeb) -> None:
    web.add("GET", URL, httpx.Response(304))
    response = client.get(URL, etag='"abc"', last_modified="Wed, 30 Sep 2026 10:00:00 GMT")
    assert response.not_modified
    sent = web.calls("/v1/jobs")[0]
    assert sent.headers["if-none-match"] == '"abc"'
    assert sent.headers["if-modified-since"] == "Wed, 30 Sep 2026 10:00:00 GMT"


def test_404_raises_not_found_without_retry(client: PoliteClient, web: FakeWeb) -> None:
    with pytest.raises(NotFound):
        client.get(URL)
    assert len(web.calls("/v1/jobs")) == 1


def test_retries_on_429_and_honours_retry_after(settings, web: FakeWeb) -> None:
    waits: list[float] = []
    replies = iter(
        [httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, json={"ok": 1})]
    )
    web.add("GET", URL, lambda _request: next(replies))
    client = PoliteClient(settings, transport=httpx.MockTransport(web.handler), sleep=waits.append)
    assert client.get(URL).json() == {"ok": 1}
    assert 7.0 in waits


def test_gives_up_after_max_attempts_on_5xx(client: PoliteClient, web: FakeWeb) -> None:
    web.add("GET", URL, httpx.Response(502))
    with pytest.raises(FetchError) as caught:
        client.get(URL)
    assert caught.value.status == 502
    assert len(web.calls("/v1/jobs")) == client.settings.http_max_attempts


def test_client_errors_are_not_retried(client: PoliteClient, web: FakeWeb) -> None:
    web.add("GET", URL, httpx.Response(403))
    with pytest.raises(FetchError):
        client.get(URL)
    assert len(web.calls("/v1/jobs")) == 1


def test_non_json_body_is_a_fetch_error(client: PoliteClient, web: FakeWeb) -> None:
    web.add("GET", URL, httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(FetchError):
        client.get(URL).json()


# ------------------------------------------------- limits and checked hops


def test_an_oversized_response_is_abandoned_without_retrying(settings, web: FakeWeb) -> None:
    settings.http_max_response_bytes = 1000
    web.add("GET", URL, httpx.Response(200, content=b"x" * 5000))
    client = PoliteClient(
        settings, transport=httpx.MockTransport(web.handler), sleep=lambda _s: None
    )
    with pytest.raises(ResponseTooLarge, match="more than 1,000 bytes"):
        client.get(URL)
    assert len(web.calls("/v1/jobs")) == 1


def test_the_size_limit_counts_decompressed_bytes(settings, web: FakeWeb) -> None:
    import gzip

    settings.http_max_response_bytes = 10_000
    bomb = gzip.compress(b"0" * 1_000_000)  # about a kilobyte on the wire
    assert len(bomb) < 2000
    web.add("GET", URL, httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"}))
    client = PoliteClient(
        settings, transport=httpx.MockTransport(web.handler), sleep=lambda _s: None
    )
    with pytest.raises(ResponseTooLarge):
        client.get(URL)


def test_a_response_that_trickles_past_the_deadline_is_abandoned(
    settings, web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drip():
        for _ in range(50):
            yield b"x"

    clock = iter(range(0, 100_000, 60))  # every look at the clock is a minute later
    monkeypatch.setattr("jobportal.http.time.monotonic", lambda: float(next(clock)))
    web.add("GET", URL, lambda _request: httpx.Response(200, content=drip()))
    client = PoliteClient(
        settings, transport=httpx.MockTransport(web.handler), sleep=lambda _s: None
    )
    with pytest.raises(ResponseTooSlow):
        client.get(URL)


def test_only_the_start_of_a_huge_robots_txt_is_read(client: PoliteClient, web: FakeWeb) -> None:
    padding = "# " + "x" * 2000 + "\n"
    web.robots[ORIGIN] = httpx.Response(
        200,
        text="User-agent: *\nDisallow: /v1/\n" + padding * 2000,  # about 4 MB
    )
    web.json("GET", URL, {})
    with pytest.raises(RobotsDisallowed):  # the rules at the top still count
        client.get(URL)


def test_a_redirect_is_asked_for_permission_like_a_first_request(
    client: PoliteClient, web: FakeWeb
) -> None:
    web.robots[ORIGIN] = httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    web.add("GET", URL, httpx.Response(302, headers={"location": "/private/jobs"}))
    web.json("GET", f"{ORIGIN}/private/jobs", {"secret": True})
    with pytest.raises(RobotsDisallowed, match="/private/jobs"):
        client.get(URL)
    assert web.calls("/private/jobs") == []

    # Another host is asked through its own robots.txt.
    other = "https://elsewhere.example.com"
    web.robots[other] = httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    web.add("GET", f"{ORIGIN}/moved", httpx.Response(301, headers={"location": f"{other}/jobs"}))
    with pytest.raises(RobotsDisallowed):
        client.get(f"{ORIGIN}/moved")
    assert [r for r in web.calls("elsewhere.example.com") if r.url.path != "/robots.txt"] == []


def test_allowed_redirects_are_followed_and_loops_are_cut(
    client: PoliteClient, web: FakeWeb
) -> None:
    web.add("GET", URL, httpx.Response(302, headers={"location": "/v2/jobs"}))
    web.json("GET", f"{ORIGIN}/v2/jobs", {"ok": True})
    response = client.get(URL)
    assert response.json() == {"ok": True} and response.url == f"{ORIGIN}/v2/jobs"

    web.add("GET", f"{ORIGIN}/loop", httpx.Response(302, headers={"location": "/loop"}))
    with pytest.raises(FetchError, match="redirected more than"):
        client.get(f"{ORIGIN}/loop")
    web.add("POST", URL, httpx.Response(307, headers={"location": "/v2/jobs"}))
    with pytest.raises(FetchError, match="answered with a redirect"):
        client.post_json(URL, {"q": 1})


def test_the_robots_cache_stays_bounded(client: PoliteClient, web: FakeWeb) -> None:
    from jobportal.http import ROBOTS_CACHE_SIZE

    for index in range(ROBOTS_CACHE_SIZE + 50):
        client.allowed(f"https://host{index}.example.com/jobs")
    assert len(client._robots) <= ROBOTS_CACHE_SIZE


# ------------------------------------------------------------------ pinning


def test_the_connection_goes_to_the_address_that_was_checked(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([("93.184.216.34",), ("127.0.0.1",)])  # a name that changes its answer
    lookups: list[str] = []

    def resolver(host: str) -> tuple[str, ...]:
        lookups.append(host)
        return next(answers)

    monkeypatch.setattr("jobportal.netguard.resolve", resolver)
    client = PoliteClient(settings, pin=True)
    try:
        target, headers, extensions = client._target("https://rebind.example:8443/careers?x=1")
        assert lookups == ["rebind.example"]  # looked up once, and that answer is the one used
        assert target == "https://93.184.216.34:8443/careers?x=1"
        assert headers == {"Host": "rebind.example:8443"}
        assert extensions == {"sni_hostname": "rebind.example"}
        with pytest.raises(UrlRefused):  # the second answer is refused, not connected to
            client._target("https://rebind.example:8443/careers")
    finally:
        client.close()


def test_a_pinned_request_reaches_the_checked_address_under_its_own_name(
    settings, form_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "pinned.test" exists only in this table: the request can succeed only
    # if the client connects to the address it was given here.
    monkeypatch.setattr(
        "jobportal.netguard.resolve",
        lambda host: ("127.0.0.1",) if host == "pinned.test" else (),
    )
    with PoliteClient(settings, pin=True, sleep=lambda _s: None) as client:
        response = client.get(f"http://pinned.test:{form_server.port}/classic.html")
    assert response.status == 200 and "<form" in response.text
    assert ("GET", "/classic.html") in form_server.requests
