"""Apply the numbered SQL files in migrations/ in order.

Run with: python -m app.migrate

Applied versions are recorded in schema_migrations. Each file runs in its own
transaction, so a failing migration leaves the database at the last good version.
"""

import re
from pathlib import Path

import psycopg

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

FILE_PATTERN = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")

BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def discover(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Return migration files in apply order, rejecting names that cannot be ordered safely."""
    paths = sorted(directory.glob("*.sql"))
    for path in paths:
        if not FILE_PATTERN.match(path.name):
            raise ValueError(f"migration file must be named like 001_name.sql: {path.name}")
    prefixes = [path.name[:3] for path in paths]
    duplicates = sorted({p for p in prefixes if prefixes.count(p) > 1})
    if duplicates:
        raise ValueError(f"duplicate migration numbers: {duplicates}")
    return paths


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration and return the versions applied by this call."""
    with conn.transaction():
        conn.execute(BOOKKEEPING_DDL)

    applied = []
    for path in discover(directory):
        version = path.stem
        with conn.transaction():
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
            ).fetchone()
            if already:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        applied.append(version)
    return applied


def main() -> None:
    with psycopg.connect(get_settings().database_url) as conn:
        applied = migrate(conn)
    if applied:
        print("applied: " + ", ".join(applied))
    else:
        print("schema is up to date")


if __name__ == "__main__":
    main()
