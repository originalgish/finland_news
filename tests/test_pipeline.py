from datetime import UTC, datetime

import psycopg
import pytest

from app.config import Settings
from worker import pipeline
from worker.fetch import FetchResult, Source
from worker.pipeline import RUN_LOCK_KEY, fetch_is_healthy, run_pipeline

FETCH_URL = "https://hc.example/fetch"


def settings():
    return Settings(
        database_url="",
        user_agent="test",
        embed_model="",
        embed_dim=384,
        embed_cache_dir=None,
        anthropic_api_key=None,
        llm_model="",
        llm_max_articles_per_run=1,
        healthcheck_fetch_url=FETCH_URL,
    )


def fetched(success):
    source = Source(
        id=1,
        name="Yle",
        feed_url="https://yle.fi/rss",
        etag=None,
        last_modified=None,
        body_hash=None,
        consecutive_failures=0,
    )
    return FetchResult(source=source, started_at=datetime.now(UTC), success=success)


def recording(calls, name, code=0):
    def stage(conn, settings):
        calls.append(name)
        return code

    return stage


def lock_is_free(url):
    """True when no run holds the lock. Checked from another session, then released."""
    with psycopg.connect(url) as other:
        free = other.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_KEY,)).fetchone()[0]
        if free:
            other.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK_KEY,))
    return free


@pytest.fixture
def pings(monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline, "ping", lambda url, *, failed=False: sent.append((url, failed)))
    return sent


@pytest.fixture
def stages(monkeypatch):
    """Replaces the follow-up stages with recorders. Returns the list of names that ran."""
    calls = []
    monkeypatch.setattr(
        pipeline,
        "FOLLOW_UP_STAGES",
        [
            ("tag", recording(calls, "tag")),
            ("embed", recording(calls, "embed")),
            ("cluster", recording(calls, "cluster")),
            ("enrich", recording(calls, "enrich")),
        ],
    )
    return calls


def fetch_returning(monkeypatch, results):
    monkeypatch.setattr(pipeline, "fetch_sources", lambda conn, settings: results)


def test_at_least_sixty_percent_of_sources_makes_fetch_healthy():
    assert fetch_is_healthy([fetched(True)] * 3 + [fetched(False)] * 2)
    assert not fetch_is_healthy([fetched(True)] * 2 + [fetched(False)] * 3)
    assert not fetch_is_healthy([])


def test_a_clean_run_pings_success_and_frees_the_lock(
    scratch_conn, scratch_url, monkeypatch, pings, stages
):
    fetch_returning(monkeypatch, [fetched(True)] * 5)

    code = run_pipeline(scratch_conn, settings())

    assert code == 0
    assert stages == ["tag", "embed", "cluster", "enrich"]
    assert pings == [(FETCH_URL, False)]
    assert lock_is_free(scratch_url)


def test_a_run_that_finds_the_lock_taken_does_nothing(
    scratch_conn, scratch_url, monkeypatch, pings, stages
):
    def must_not_fetch(conn, settings):
        raise AssertionError("a skipped run must not fetch")

    monkeypatch.setattr(pipeline, "fetch_sources", must_not_fetch)
    holder = psycopg.connect(scratch_url)
    holder.execute("SELECT pg_advisory_lock(%s)", (RUN_LOCK_KEY,))
    holder.commit()
    try:
        code = run_pipeline(scratch_conn, settings())
    finally:
        holder.close()

    assert code == 0
    assert stages == []
    assert pings == []


def test_a_failing_stage_stops_the_chain_and_reports_failure(
    scratch_conn, scratch_url, monkeypatch, pings
):
    calls = []
    monkeypatch.setattr(
        pipeline,
        "FOLLOW_UP_STAGES",
        [
            ("tag", recording(calls, "tag")),
            ("embed", recording(calls, "embed", code=1)),
            ("cluster", recording(calls, "cluster")),
        ],
    )
    fetch_returning(monkeypatch, [fetched(True)] * 5)

    code = run_pipeline(scratch_conn, settings())

    assert code == 1
    assert calls == ["tag", "embed"]
    assert pings == [(FETCH_URL, True)]
    assert lock_is_free(scratch_url)


def test_an_exception_rolls_back_the_stage_and_still_frees_the_lock(
    scratch_conn, scratch_url, monkeypatch, pings
):
    def breaks_the_transaction(conn, settings):
        conn.execute("SELECT 1/0")  # leaves the transaction aborted

    monkeypatch.setattr(pipeline, "FOLLOW_UP_STAGES", [("tag", breaks_the_transaction)])
    fetch_returning(monkeypatch, [fetched(True)] * 5)

    with pytest.raises(psycopg.errors.DivisionByZero):
        run_pipeline(scratch_conn, settings())

    assert pings == [(FETCH_URL, True)]
    assert lock_is_free(scratch_url)
    assert scratch_conn.execute("SELECT 1").fetchone() == (1,)


def test_too_few_sources_fetched_runs_the_chain_but_reports_failure(
    monkeypatch, pings, stages, scratch_conn
):
    fetch_returning(monkeypatch, [fetched(True)] * 2 + [fetched(False)] * 3)

    code = run_pipeline(scratch_conn, settings())

    assert code == 0
    assert stages == ["tag", "embed", "cluster", "enrich"]
    assert pings == [(FETCH_URL, True)]


def test_a_failed_fetch_stops_before_any_stage_and_reports_failure(
    monkeypatch, pings, stages, scratch_conn
):
    def network_down(conn, settings):
        raise RuntimeError("network down")

    monkeypatch.setattr(pipeline, "fetch_sources", network_down)

    with pytest.raises(RuntimeError, match="network down"):
        run_pipeline(scratch_conn, settings())

    assert stages == []
    assert pings == [(FETCH_URL, True)]
