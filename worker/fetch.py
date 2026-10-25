"""Stage 1: download each enabled feed and parse it.

Every source gets a conditional GET (ETag and Last-Modified). A body whose hash
matches the stored one is recorded as `unchanged` and not parsed again. Each
attempt writes one fetch_log row and updates the source's fetch state. Parsed
entries are returned in memory for the next stage.
"""

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import feedparser
import httpx
import psycopg
from psycopg.rows import class_row

MAX_CONSECUTIVE_FAILURES = 5
CONCURRENCY = 5
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

REDIRECT_CODES = {301, 302, 307, 308}
PERMANENT_REDIRECT_CODES = {301, 308}

LOAD_SOURCES_SQL = """
SELECT id, name, feed_url, etag, last_modified, body_hash, consecutive_failures
FROM sources
WHERE enabled
ORDER BY id
"""

# Item count from the last run that parsed new content. Used to spot a feed that
# suddenly comes back empty.
LAST_ITEM_COUNTS_SQL = """
SELECT DISTINCT ON (source_id) source_id, items_found
FROM fetch_log
WHERE outcome = 'ok'
ORDER BY source_id, started_at DESC
"""

INSERT_LOG_SQL = """
INSERT INTO fetch_log (source_id, started_at, duration_ms, status_code, outcome,
                       items_found, items_new, error)
VALUES (%(source_id)s, %(started_at)s, %(duration_ms)s, %(status_code)s, %(outcome)s,
        %(items_found)s, 0, %(error)s)
"""

# COALESCE keeps the stored value when a result has nothing new to say.
UPDATE_SOURCE_SQL = """
UPDATE sources
SET last_fetched_at = %(started_at)s,
    last_status = %(status_code)s,
    consecutive_failures = %(failures)s,
    enabled = %(enabled)s,
    disabled_reason = %(disabled_reason)s,
    feed_url = COALESCE(%(new_feed_url)s, feed_url),
    etag = COALESCE(%(etag)s, etag),
    last_modified = COALESCE(%(last_modified)s, last_modified),
    body_hash = COALESCE(%(body_hash)s, body_hash)
WHERE id = %(source_id)s
"""


@dataclass
class Source:
    id: int
    name: str
    feed_url: str
    etag: str | None
    last_modified: str | None
    body_hash: str | None
    consecutive_failures: int


@dataclass
class FetchOptions:
    user_agent: str
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rng: random.Random = field(default_factory=random.Random)


@dataclass
class FetchResult:
    source: Source
    started_at: datetime
    outcome: str = "error"  # ok | not_modified | unchanged | empty | error
    success: bool = False
    status_code: int | None = None
    duration_ms: int = 0
    items_found: int = 0
    error: str | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    new_feed_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    body_hash: str | None = None


def run_fetch(conn: psycopg.Connection, options: FetchOptions) -> list[FetchResult]:
    """Fetch every enabled source, record the results, and return them."""
    sources = load_enabled_sources(conn)
    previous_items = last_item_counts(conn)
    results = asyncio.run(fetch_all(sources, previous_items, options))
    record_results(conn, results)
    return results


async def fetch_all(
    sources: list[Source], previous_items: dict[int, int], options: FetchOptions
) -> list[FetchResult]:
    gate = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:

        async def one(source: Source) -> FetchResult:
            async with gate:
                return await fetch_source(
                    client, source, previous_items.get(source.id, 0), options
                )

        return list(await asyncio.gather(*(one(s) for s in sources)))


async def fetch_source(
    client: httpx.AsyncClient, source: Source, previous_items: int, options: FetchOptions
) -> FetchResult:
    result = FetchResult(source=source, started_at=datetime.now(UTC))
    started = time.perf_counter()

    headers = {"User-Agent": options.user_agent}
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified

    try:
        await _fetch(client, source, headers, previous_items, options, result)
    except httpx.TransportError as exc:
        _fail(result, "error", f"{type(exc).__name__}: {exc}")

    result.duration_ms = round((time.perf_counter() - started) * 1000)
    return result


async def _fetch(
    client: httpx.AsyncClient,
    source: Source,
    headers: dict[str, str],
    previous_items: int,
    options: FetchOptions,
    result: FetchResult,
) -> None:
    response = await _get(client, source.feed_url, headers, options)
    result.status_code = response.status_code

    if response.status_code in REDIRECT_CODES:
        target = _redirect_target(source.feed_url, response)
        if target is None:
            _fail(result, "error", "redirect without Location header")
            return
        if response.status_code in PERMANENT_REDIRECT_CODES:
            result.new_feed_url = target
        # Follow one hop only. A second redirect is treated as an error.
        response = await _get(client, target, headers, options)
        result.status_code = response.status_code
        if response.status_code in REDIRECT_CODES:
            _fail(result, "error", "more than one redirect")
            return

    if response.status_code == 304:
        _succeed(result, "not_modified")
    elif response.status_code == 200:
        _accept_body(response, source, previous_items, result)
    else:
        _fail(result, "error", f"HTTP {response.status_code}")


def _accept_body(
    response: httpx.Response,
    source: Source,
    previous_items: int,
    result: FetchResult,
) -> None:
    body_hash = hashlib.sha256(response.content).hexdigest()
    if body_hash == source.body_hash:
        _store_state(result, response, body_hash)
        _succeed(result, "unchanged")
        return

    feed = feedparser.parse(response.content)
    if feed.entries:
        result.entries = list(feed.entries)
        result.items_found = len(feed.entries)
        _store_state(result, response, body_hash)
        _succeed(result, "ok")
    elif feed.bozo or not feed.version:
        # No recognised RSS or Atom format (for example an HTML error page), or broken XML.
        reason = feed.get("bozo_exception") or "not RSS or Atom"
        _fail(result, "error", f"unparseable feed: {reason}")
    elif previous_items > 0:
        # Never read an empty feed as "everything was deleted". Keep the old state.
        _fail(result, "empty", f"feed has no items; the last good run had {previous_items}")
    else:
        _store_state(result, response, body_hash)
        _succeed(result, "empty")


def _store_state(result: FetchResult, response: httpx.Response, body_hash: str) -> None:
    result.etag = response.headers.get("etag")
    result.last_modified = response.headers.get("last-modified")
    result.body_hash = body_hash


def _succeed(result: FetchResult, outcome: str) -> None:
    result.outcome = outcome
    result.success = True


def _fail(result: FetchResult, outcome: str, error: str) -> None:
    result.outcome = outcome
    result.success = False
    result.error = error


def _redirect_target(url: str, response: httpx.Response) -> str | None:
    location = response.headers.get("location")
    return urljoin(url, location) if location else None


def _is_retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


async def _get(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    options: FetchOptions,
) -> httpx.Response:
    """GET, retrying timeouts, connection errors, 429 and 5xx with backoff."""
    for attempt in range(options.max_retries + 1):
        is_last = attempt == options.max_retries
        try:
            response = await client.get(url, headers=headers)
        except httpx.TransportError:
            if is_last:
                raise
            await options.sleep(_retry_delay(attempt, None, options))
            continue
        if is_last or not _is_retryable(response.status_code):
            return response
        await options.sleep(_retry_delay(attempt, response, options))


def _retry_delay(attempt: int, response: httpx.Response | None, options: FetchOptions) -> float:
    if response is not None:
        retry_after = _parse_retry_after(response.headers.get("retry-after"))
        if retry_after is not None:
            return min(retry_after, options.max_delay)
    backoff = options.base_delay * 2**attempt
    return min(options.max_delay, backoff * (0.5 + options.rng.random()))


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either a number of seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def load_enabled_sources(conn: psycopg.Connection) -> list[Source]:
    with conn.cursor(row_factory=class_row(Source)) as cur:
        cur.execute(LOAD_SOURCES_SQL)
        return cur.fetchall()


def last_item_counts(conn: psycopg.Connection) -> dict[int, int]:
    return dict(conn.execute(LAST_ITEM_COUNTS_SQL).fetchall())


def record_results(conn: psycopg.Connection, results: list[FetchResult]) -> None:
    with conn.transaction():
        for result in results:
            failures = 0 if result.success else result.source.consecutive_failures + 1
            disabled = failures >= MAX_CONSECUTIVE_FAILURES
            reason = None
            if disabled:
                reason = f"{failures} consecutive failures; last error: {result.error}"
            params = {
                "source_id": result.source.id,
                "started_at": result.started_at,
                "duration_ms": result.duration_ms,
                "status_code": result.status_code,
                "outcome": result.outcome,
                "items_found": result.items_found,
                "error": result.error,
                "failures": failures,
                "enabled": not disabled,
                "disabled_reason": reason,
                "new_feed_url": result.new_feed_url,
                "etag": result.etag,
                "last_modified": result.last_modified,
                "body_hash": result.body_hash,
            }
            conn.execute(INSERT_LOG_SQL, params)
            conn.execute(UPDATE_SOURCE_SQL, params)
