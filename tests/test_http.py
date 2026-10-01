from __future__ import annotations

import httpx
import pytest

from jobportal.http import FetchError, NotFound, PoliteClient, RobotsDisallowed
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
