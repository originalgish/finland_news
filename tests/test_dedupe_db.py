from datetime import UTC, datetime

import pytest

from app.migrate import migrate
from worker.dedupe import store_articles
from worker.normalize import Article

PUBLISHED = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


@pytest.fixture
def conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def source_id(conn, name):
    return conn.execute("SELECT id FROM sources WHERE name = %s", (name,)).fetchone()[0]


def article(url, title="Otsikko", guid=None, summary=None):
    return Article(
        url=url,
        url_canonical=url,
        guid=guid,
        title=title,
        summary=summary,
        feed_categories=["Politiikka"],
        published_at=PUBLISHED,
    )


def count_articles(conn):
    return conn.execute("SELECT count(*) FROM articles").fetchone()[0]


def test_storing_the_same_articles_twice_inserts_nothing_the_second_time(conn):
    yle = source_id(conn, "Yle")
    batch = [
        article("https://yle.fi/a/1", guid="a1"),
        article("https://yle.fi/a/2", guid="a2"),
        article("https://yle.fi/a/3", guid="a3"),
    ]

    assert store_articles(conn, yle, batch) == 3
    assert store_articles(conn, yle, batch) == 0
    assert count_articles(conn) == 3


def test_the_same_canonical_url_from_two_feeds_is_one_row(conn):
    url = "https://www.iltalehti.fi/kotimaa/a/1"

    store_articles(conn, source_id(conn, "Iltalehti"), [article(url)])
    inserted = store_articles(conn, source_id(conn, "Kauppalehti"), [article(url)])

    assert inserted == 0
    assert count_articles(conn) == 1


def test_a_url_repeated_within_one_batch_counts_once(conn):
    url = "https://www.hs.fi/a.html"

    inserted = store_articles(conn, source_id(conn, "Helsingin Sanomat"), [article(url), article(url)])

    assert inserted == 1
    assert count_articles(conn) == 1


def test_edited_headline_updates_the_row_and_clears_enriched_at(conn):
    hs = source_id(conn, "Helsingin Sanomat")
    url = "https://www.hs.fi/a.html"
    store_articles(conn, hs, [article(url, title="Vanha otsikko")])
    conn.execute("UPDATE articles SET enriched_at = now(), title_en = 'Old headline'")

    inserted = store_articles(conn, hs, [article(url, title="Uusi otsikko")])

    title, enriched_at = conn.execute("SELECT title, enriched_at FROM articles").fetchone()
    assert inserted == 0
    assert count_articles(conn) == 1
    assert title == "Uusi otsikko"
    assert enriched_at is None


def test_unchanged_headline_keeps_enriched_at(conn):
    hs = source_id(conn, "Helsingin Sanomat")
    url = "https://www.hs.fi/a.html"
    store_articles(conn, hs, [article(url, summary="Vanha")])
    conn.execute("UPDATE articles SET enriched_at = now()")

    store_articles(conn, hs, [article(url, summary="Uusi tiivistelmä")])

    summary, enriched_at = conn.execute("SELECT summary, enriched_at FROM articles").fetchone()
    assert summary == "Uusi tiivistelmä"
    assert enriched_at is not None


def test_known_guid_with_a_new_url_moves_the_existing_row(conn):
    hs = source_id(conn, "Helsingin Sanomat")
    store_articles(conn, hs, [article("https://www.hs.fi/old.html", guid="art-1")])

    inserted = store_articles(conn, hs, [article("https://www.hs.fi/new.html", guid="art-1")])

    assert inserted == 0
    assert count_articles(conn) == 1
    assert conn.execute("SELECT url_canonical FROM articles").fetchone()[0] == "https://www.hs.fi/new.html"


def test_the_same_guid_in_different_sources_is_two_rows(conn):
    store_articles(conn, source_id(conn, "Yle"), [article("https://yle.fi/a", guid="1")])
    store_articles(conn, source_id(conn, "Ilta-Sanomat"), [article("https://www.is.fi/a", guid="1")])

    assert count_articles(conn) == 2
