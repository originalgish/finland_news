from pathlib import Path

import httpx
import pytest
import respx

from app.migrate import migrate
from worker.fetch import FetchOptions, run_fetch

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
YLE_URL = "https://yle.fi/rss/uutiset/paauutiset"
FEEDS = {
    YLE_URL: "yle.xml",
    "https://www.hs.fi/rss/tuoreimmat.xml": "helsingin_sanomat.xml",
    "https://www.is.fi/rss/tuoreimmat.xml": "ilta_sanomat.xml",
    "https://www.iltalehti.fi/rss/uutiset.xml": "iltalehti.xml",
    "https://feeds.kauppalehti.fi/rss/main": "kauppalehti.xml",
}
EXPECTED_ITEMS = {
    "Yle": 10,
    "Helsingin Sanomat": 99,
    "Ilta-Sanomat": 100,
    "Iltalehti": 20,
    "Kauppalehti": 10,
}


async def no_wait(_delay: float) -> None:
    return None


def options() -> FetchOptions:
    return FetchOptions(user_agent="test-agent", base_delay=0.0, sleep=no_wait)


def serve_feeds(router, status_overrides=None, skip=()):
    """Mock every seeded feed with its fixture, except the URLs in `skip`."""
    status_overrides = status_overrides or {}
    for url, filename in FEEDS.items():
        if url in skip:
            continue
        if url in status_overrides:
            router.get(url).mock(return_value=httpx.Response(status_overrides[url]))
        else:
            router.get(url).mock(
                return_value=httpx.Response(
                    200,
                    content=(FIXTURES / filename).read_bytes(),
                    headers={"ETag": f'"{filename}"'},
                )
            )


@pytest.fixture
def migrated_conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def test_first_run_stores_items_and_second_run_is_unchanged(migrated_conn):
    with respx.mock() as router:
        serve_feeds(router)
        first = run_fetch(migrated_conn, options())

    assert {r.source.name: r.items_found for r in first} == EXPECTED_ITEMS
    assert {r.outcome for r in first} == {"ok"}
    stored = migrated_conn.execute("SELECT count(*) FROM articles").fetchone()[0]
    assert sum(r.items_new for r in first) == stored > 0

    with respx.mock() as router:
        serve_feeds(router)
        second = run_fetch(migrated_conn, options())

    assert {r.outcome for r in second} == {"unchanged"}
    assert sum(r.items_new for r in second) == 0
    logged = migrated_conn.execute("SELECT count(*) FROM fetch_log").fetchone()[0]
    assert logged == 2 * len(FEEDS)


def test_parsing_the_same_feeds_again_inserts_no_rows(migrated_conn):
    with respx.mock() as router:
        serve_feeds(router)
        run_fetch(migrated_conn, options())
    stored = migrated_conn.execute("SELECT count(*) FROM articles").fetchone()[0]
    # Forget the stored fingerprints so the same bodies are parsed again.
    migrated_conn.execute("UPDATE sources SET body_hash = NULL, etag = NULL, last_modified = NULL")

    with respx.mock() as router:
        serve_feeds(router)
        again = run_fetch(migrated_conn, options())

    assert {r.outcome for r in again} == {"ok"}
    assert sum(r.items_found for r in again) > 0
    assert sum(r.items_new for r in again) == 0
    assert migrated_conn.execute("SELECT count(*) FROM articles").fetchone()[0] == stored


def test_five_consecutive_failures_disable_a_source(migrated_conn):
    for _ in range(5):
        with respx.mock() as router:
            serve_feeds(router, status_overrides={YLE_URL: 404})
            run_fetch(migrated_conn, options())

    enabled, failures, reason = migrated_conn.execute(
        "SELECT enabled, consecutive_failures, disabled_reason FROM sources WHERE name = 'Yle'"
    ).fetchone()
    assert enabled is False
    assert failures == 5
    assert "5 consecutive failures" in reason

    # A disabled source is not requested at all. Any request to it would fail
    # because the route is not mocked.
    with respx.mock(assert_all_mocked=True) as router:
        serve_feeds(router, skip=(YLE_URL,))
        results = run_fetch(migrated_conn, options())

    assert "Yle" not in [r.source.name for r in results]
    yle_rows = migrated_conn.execute(
        "SELECT count(*) FROM fetch_log WHERE source_id = (SELECT id FROM sources WHERE name = 'Yle')"
    ).fetchone()[0]
    assert yle_rows == 5


def test_a_success_resets_the_failure_count(migrated_conn):
    with respx.mock() as router:
        serve_feeds(router, status_overrides={YLE_URL: 404})
        run_fetch(migrated_conn, options())
    with respx.mock() as router:
        serve_feeds(router)
        run_fetch(migrated_conn, options())

    failures, reason = migrated_conn.execute(
        "SELECT consecutive_failures, disabled_reason FROM sources WHERE name = 'Yle'"
    ).fetchone()
    assert failures == 0
    assert reason is None
