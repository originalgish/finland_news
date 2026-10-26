"""Stage 8: the scheduled run. Fetch, then the rest of the pipeline, behind one lock.

The order is fetch (which also normalizes and dedupes), tag, embed, cluster, enrich
(which also writes the cluster headlines). Each stage works only on rows whose marker
is still NULL, so a stage that stops is picked up where it left off on the next run.

A Postgres advisory lock is held for the whole run. A run that finds it taken does
nothing, so two runs never overlap.

The healthchecks.io check for fetching gets a success ping only when the chain finished
and at least FETCH_MIN_PERCENT of the enabled sources were fetched. Any other outcome,
including a stage that raised, sends the /fail signal, so the alert does not have to
wait for a missed ping.
"""

from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from app.config import Settings
from worker.fetch import FetchResult
from worker.health import ping
from worker.stages import (
    cluster_stage,
    embed_stage,
    enrich_stage,
    fetch_sources,
    tag_stage,
)

Stage = Callable[[psycopg.Connection, Settings], int]

# Any fixed number works, as long as every run uses the same one.
RUN_LOCK_KEY = 8_151_001
FETCH_MIN_PERCENT = 60

# The stages after fetch, in order. Each one returns 0 on success.
FOLLOW_UP_STAGES: list[tuple[str, Stage]] = [
    ("tag", tag_stage),
    ("embed", embed_stage),
    ("cluster", cluster_stage),
    ("enrich", enrich_stage),
]


class StageFailed(Exception):
    """A stage returned a non-zero exit code, so the run stops there."""


@dataclass
class RunOutcome:
    fetch_ok: bool = False
    completed: bool = False


def fetch_is_healthy(results: list[FetchResult]) -> bool:
    """True when at least FETCH_MIN_PERCENT of the enabled sources were fetched."""
    if not results:
        return False
    succeeded = sum(result.success for result in results)
    return succeeded * 100 >= len(results) * FETCH_MIN_PERCENT


def try_lock(conn: psycopg.Connection) -> bool:
    locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_KEY,)).fetchone()[0]
    conn.commit()
    return locked


def unlock(conn: psycopg.Connection) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK_KEY,))
    conn.commit()


def _run_chain(conn: psycopg.Connection, settings: Settings, outcome: RunOutcome) -> None:
    results = fetch_sources(conn, settings)
    outcome.fetch_ok = fetch_is_healthy(results)
    for name, stage in FOLLOW_UP_STAGES:
        print(f"-- {name}")
        if stage(conn, settings) != 0:
            raise StageFailed(name)
    outcome.completed = True


def run_pipeline(conn: psycopg.Connection, settings: Settings) -> int:
    if not try_lock(conn):
        print("another run is in progress, so this one is skipped")
        return 0

    outcome = RunOutcome()
    code = 1
    try:
        _run_chain(conn, settings, outcome)
        code = 0
    except StageFailed as failure:
        print(f"stopped: the {failure} stage failed")
    finally:
        # Runs on every path. An unexpected exception still propagates, so its traceback
        # reaches the cron log, after the lock is freed and the failure is reported.
        # A failed stage may leave its transaction aborted, so roll back first.
        conn.rollback()
        unlock(conn)
        ping(settings.healthcheck_fetch_url, failed=not (outcome.completed and outcome.fetch_ok))
    return code
