import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest
import respx

from worker.fetch import FetchOptions, Source, _parse_retry_after, fetch_source

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
YLE_URL = "https://yle.fi/rss/uutiset/paauutiset"
YLE_XML = (FIXTURES / "yle.xml").read_bytes()
EMPTY_RSS = b'<rss version="2.0"><channel><title>empty</title></channel></rss>'


def source(**overrides) -> Source:
    fields = {
        "id": 1,
        "name": "Yle",
        "feed_url": YLE_URL,
        "etag": None,
        "last_modified": None,
        "body_hash": None,
        "consecutive_failures": 0,
    }
    return Source(**(fields | overrides))


def options() -> tuple[FetchOptions, list[float]]:
    """Options whose sleep records delays instead of waiting."""
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    return FetchOptions(user_agent="test-agent", base_delay=1.0, sleep=record), delays


def fetch(src: Source, previous_items: int = 0, opts: FetchOptions | None = None):
    opts = opts or options()[0]

    async def run():
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            return await fetch_source(client, src, previous_items, opts)

    return asyncio.run(run())


@respx.mock
def test_304_is_not_modified_and_sends_conditional_headers():
    route = respx.get(YLE_URL).mock(return_value=httpx.Response(304))

    result = fetch(source(etag='"abc"', last_modified="Sun, 25 Oct 2026 10:00:00 GMT"))

    assert result.outcome == "not_modified"
    assert result.success
    sent = route.calls[-1].request.headers
    assert sent["If-None-Match"] == '"abc"'
    assert sent["If-Modified-Since"] == "Sun, 25 Oct 2026 10:00:00 GMT"
    assert sent["User-Agent"] == "test-agent"


@respx.mock
def test_same_body_hash_is_unchanged_and_not_parsed():
    respx.get(YLE_URL).mock(
        return_value=httpx.Response(200, content=YLE_XML, headers={"ETag": '"v2"'})
    )

    result = fetch(source(body_hash=hashlib.sha256(YLE_XML).hexdigest()))

    assert result.outcome == "unchanged"
    assert result.success
    assert result.entries == []
    assert result.etag == '"v2"'


@respx.mock
def test_new_body_is_parsed_and_its_state_is_kept():
    respx.get(YLE_URL).mock(
        return_value=httpx.Response(
            200,
            content=YLE_XML,
            headers={"ETag": '"v1"', "Last-Modified": "Sun, 25 Oct 2026 10:00:00 GMT"},
        )
    )

    result = fetch(source())

    assert result.outcome == "ok"
    assert result.success
    assert result.items_found == 10
    assert len(result.entries) == 10
    assert result.body_hash == hashlib.sha256(YLE_XML).hexdigest()
    assert result.etag == '"v1"'
    assert result.last_modified == "Sun, 25 Oct 2026 10:00:00 GMT"


@respx.mock
def test_permanent_redirect_records_new_url_and_retries_once():
    new_url = "https://yle.fi/uutiset/rss/new.xml"
    respx.get(YLE_URL).mock(return_value=httpx.Response(301, headers={"Location": new_url}))
    respx.get(new_url).mock(return_value=httpx.Response(200, content=YLE_XML))

    result = fetch(source())

    assert result.success
    assert result.new_feed_url == new_url


@respx.mock
def test_temporary_redirect_is_followed_without_recording_new_url():
    target = "https://yle.fi/tilapainen.xml"
    respx.get(YLE_URL).mock(return_value=httpx.Response(302, headers={"Location": target}))
    respx.get(target).mock(return_value=httpx.Response(200, content=YLE_XML))

    result = fetch(source())

    assert result.success
    assert result.new_feed_url is None


@respx.mock
def test_second_redirect_is_an_error():
    target = "https://yle.fi/second"
    respx.get(YLE_URL).mock(return_value=httpx.Response(302, headers={"Location": target}))
    hop = respx.get(target).mock(
        return_value=httpx.Response(302, headers={"Location": "https://yle.fi/third"})
    )

    result = fetch(source())

    assert not result.success
    assert result.error == "more than one redirect"
    assert hop.call_count == 1


@respx.mock
def test_server_errors_are_retried_then_succeed():
    route = respx.get(YLE_URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(503),
            httpx.Response(200, content=YLE_XML),
        ]
    )
    opts, delays = options()

    result = fetch(source(), opts=opts)

    assert result.success
    assert route.call_count == 3
    assert len(delays) == 2


@respx.mock
def test_server_errors_give_up_after_three_retries():
    route = respx.get(YLE_URL).mock(return_value=httpx.Response(500))
    opts, delays = options()

    result = fetch(source(), opts=opts)

    assert not result.success
    assert result.outcome == "error"
    assert route.call_count == 4
    assert len(delays) == 3


@respx.mock
def test_retry_after_header_is_honoured():
    respx.get(YLE_URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(304)]
    )
    opts, delays = options()

    result = fetch(source(etag='"x"'), opts=opts)

    assert result.success
    assert delays == [7.0]


@respx.mock
def test_timeouts_are_retried_then_reported():
    route = respx.get(YLE_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    opts, delays = options()

    result = fetch(source(), opts=opts)

    assert not result.success
    assert result.outcome == "error"
    assert result.error.startswith("ReadTimeout")
    assert route.call_count == 4
    assert len(delays) == 3


@pytest.mark.parametrize("status", [403, 404, 410])
@respx.mock
def test_client_errors_are_not_retried(status):
    route = respx.get(YLE_URL).mock(return_value=httpx.Response(status))
    opts, delays = options()

    result = fetch(source(), opts=opts)

    assert not result.success
    assert result.status_code == status
    assert route.call_count == 1
    assert delays == []


@respx.mock
def test_empty_feed_after_items_is_a_failure_and_keeps_no_state():
    respx.get(YLE_URL).mock(return_value=httpx.Response(200, content=EMPTY_RSS))

    result = fetch(source(), previous_items=10)

    assert result.outcome == "empty"
    assert not result.success
    assert result.body_hash is None


@respx.mock
def test_empty_feed_without_history_is_accepted():
    respx.get(YLE_URL).mock(return_value=httpx.Response(200, content=EMPTY_RSS))

    result = fetch(source(), previous_items=0)

    assert result.outcome == "empty"
    assert result.success
    assert result.body_hash is not None


@respx.mock
def test_unparseable_body_is_an_error():
    respx.get(YLE_URL).mock(
        return_value=httpx.Response(200, content=b"<html><body>Maintenance</body></html>")
    )

    result = fetch(source(), previous_items=10)

    assert result.outcome == "error"
    assert not result.success
    assert result.error.startswith("unparseable feed")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("7", 7.0),
        (None, None),
        ("nonsense", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),  # date in the past
    ],
)
def test_parse_retry_after(value, expected):
    assert _parse_retry_after(value) == expected
