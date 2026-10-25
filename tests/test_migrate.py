import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app.migrate import MIGRATIONS_DIR, discover, migrate


def write(directory, name, body="SELECT 1;"):
    (directory / name).write_text(body)


def test_discover_orders_files_by_number(tmp_path):
    write(tmp_path, "002_second.sql")
    write(tmp_path, "001_first.sql")

    assert [p.name for p in discover(tmp_path)] == ["001_first.sql", "002_second.sql"]


def test_discover_rejects_duplicate_numbers(tmp_path):
    write(tmp_path, "001_a.sql")
    write(tmp_path, "001_b.sql")

    with pytest.raises(ValueError, match="duplicate"):
        discover(tmp_path)


def test_discover_rejects_names_without_number(tmp_path):
    write(tmp_path, "init.sql")

    with pytest.raises(ValueError, match="001_name.sql"):
        discover(tmp_path)


def test_shipped_migrations_are_discoverable():
    names = [p.name for p in discover(MIGRATIONS_DIR)]

    assert names[0] == "001_init.sql"


@pytest.fixture
def scratch_conn():
    """A connection to a freshly created database, dropped afterwards.

    Needs a role allowed to CREATE DATABASE, which the compose superuser is.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    name = f"migrate_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(make_conninfo(url, dbname=name)) as conn:
            yield conn
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_migrate_creates_schema_and_is_idempotent(scratch_conn):
    first = migrate(scratch_conn)
    second = migrate(scratch_conn)

    assert first == ["001_init"]
    assert second == []

    tables = {
        row[0]
        for row in scratch_conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        )
    }
    assert {"sources", "articles", "clusters", "tags", "views", "digests"} <= tables
