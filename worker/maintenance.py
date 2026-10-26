"""Nightly housekeeping: drop old fetch_log rows, then refresh the table statistics.

fetch_log gains a row per source per run, and the source-health page only reads the
latest few, so rows older than RETENTION_DAYS are never shown. VACUUM ANALYZE keeps
the planner's statistics current after the deletes and the steady stream of inserts.
"""

import psycopg

RETENTION_DAYS = 90

PRUNE_SQL = "DELETE FROM fetch_log WHERE started_at < now() - make_interval(days => %s)"


def run_maintenance(conn: psycopg.Connection) -> int:
    """Delete the old fetch_log rows and analyze the database. Returns the rows deleted."""
    pruned = conn.execute(PRUNE_SQL, (RETENTION_DAYS,)).rowcount
    conn.commit()
    # VACUUM cannot run inside a transaction block, so switch to autocommit first.
    conn.autocommit = True
    conn.execute("VACUUM ANALYZE")
    return pruned
