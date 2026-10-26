from datetime import UTC, datetime, timedelta

import pytest

from app.migrate import migrate
from worker.maintenance import RETENTION_DAYS, run_maintenance

NOW = datetime.now(UTC)


@pytest.fixture
def conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def log_fetch(conn, days_ago):
    source_id = conn.execute("SELECT id FROM sources WHERE name = 'Yle'").fetchone()[0]
    conn.execute(
        "INSERT INTO fetch_log (source_id, started_at, outcome) VALUES (%s, %s, 'ok')",
        (source_id, NOW - timedelta(days=days_ago)),
    )
    conn.commit()


def count_fetch_log(conn):
    return conn.execute("SELECT count(*) FROM fetch_log").fetchone()[0]


def test_rows_older_than_the_retention_are_pruned(conn):
    log_fetch(conn, RETENTION_DAYS + 10)
    log_fetch(conn, 10)

    pruned = run_maintenance(conn)

    assert pruned == 1
    assert count_fetch_log(conn) == 1


def test_a_row_inside_the_retention_is_kept(conn):
    log_fetch(conn, RETENTION_DAYS - 1)

    assert run_maintenance(conn) == 0
    assert count_fetch_log(conn) == 1
