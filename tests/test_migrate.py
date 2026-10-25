import pytest

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


def test_migrate_creates_schema_and_is_idempotent(scratch_conn):
    first = migrate(scratch_conn)
    second = migrate(scratch_conn)

    assert first == ["001_init", "002_seed_sources"]
    assert second == []

    tables = {
        row[0]
        for row in scratch_conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        )
    }
    assert {"sources", "articles", "clusters", "tags", "views", "digests"} <= tables


def test_seed_loads_outlets_tags_and_category_map(scratch_conn):
    migrate(scratch_conn)

    sources = {
        row[0]: row[1]
        for row in scratch_conn.execute("SELECT name, feed_url FROM sources")
    }
    assert set(sources) == {"Yle", "Helsingin Sanomat", "Ilta-Sanomat", "Iltalehti", "Kauppalehti"}
    assert all(url.startswith("https://") for url in sources.values())

    tags = {row[0] for row in scratch_conn.execute("SELECT name FROM tags")}
    assert tags == {
        "politics", "economy", "business", "sports", "culture", "crime",
        "health", "tech", "world", "local", "weather",
    }

    mapped = scratch_conn.execute("SELECT count(*) FROM feed_category_map").fetchone()[0]
    assert mapped > 0
