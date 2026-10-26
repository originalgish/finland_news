from datetime import UTC, datetime

import pytest

from app.migrate import migrate
from worker.tagging import apply_tags

PUBLISHED = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


@pytest.fixture
def conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def add_article(conn, index, title, categories=(), source="Helsingin Sanomat"):
    source_id = conn.execute("SELECT id FROM sources WHERE name = %s", (source,)).fetchone()[0]
    url = f"https://example.fi/t/{index}"
    return conn.execute(
        """
        INSERT INTO articles (source_id, url, url_canonical, title, published_at, feed_categories)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (source_id, url, url, title, PUBLISHED, list(categories)),
    ).fetchone()[0]


def tags_of(conn, article_id):
    return dict(
        conn.execute(
            """
            SELECT t.name, at.origin::text FROM article_tags at
            JOIN tags t ON t.id = at.tag_id WHERE at.article_id = %s
            """,
            (article_id,),
        ).fetchall()
    )


def tag_id(conn, name):
    return conn.execute("SELECT id FROM tags WHERE name = %s", (name,)).fetchone()[0]


def test_a_feed_category_maps_to_its_tag(conn):
    article = add_article(conn, 1, "Maalitykset", categories=["Urheilu"])

    summary = apply_tags(conn)

    assert tags_of(conn, article) == {"sports": "feed"}
    assert summary.feed == 1


def test_a_category_mapping_only_applies_to_its_own_source(conn):
    # The seed maps 'Maailma' for Helsingin Sanomat only. Yle has no mapping for it.
    article = add_article(conn, 1, "Yle-juttu", categories=["Maailma"], source="Yle")

    apply_tags(conn)

    assert tags_of(conn, article) == {}


def test_a_mapping_without_a_source_applies_to_every_source(conn):
    conn.execute(
        "INSERT INTO feed_category_map (source_id, category, tag_id) VALUES (NULL, 'Sää', %s)",
        (tag_id(conn, "weather"),),
    )
    article = add_article(conn, 1, "Lämpenee", categories=["Sää"], source="Iltalehti")

    apply_tags(conn)

    assert tags_of(conn, article) == {"weather": "feed"}


def test_an_unmapped_category_adds_nothing(conn):
    article = add_article(conn, 1, "Muu aihe", categories=["Mielipide"])

    apply_tags(conn)

    assert tags_of(conn, article) == {}


def test_a_keyword_rule_tags_matching_articles(conn):
    conn.execute(
        "INSERT INTO tag_rules (tag_id, query) VALUES (%s, 'myrsky')",
        (tag_id(conn, "weather"),),
    )
    matching = add_article(conn, 1, "Myrsky tuli Suomeen")
    other = add_article(conn, 2, "Kuumat kesäpäivät")

    summary = apply_tags(conn)

    assert tags_of(conn, matching) == {"weather": "rule"}
    assert tags_of(conn, other) == {}
    assert summary.rules == 1


def test_applying_the_rules_again_adds_nothing(conn):
    conn.execute("INSERT INTO tag_rules (tag_id, query) VALUES (%s, 'myrsky')", (tag_id(conn, "weather"),))
    add_article(conn, 1, "Myrsky tuli Suomeen")
    apply_tags(conn)

    summary = apply_tags(conn)

    assert summary.rules == 0
    assert conn.execute("SELECT count(*) FROM article_tags").fetchone()[0] == 1


def test_a_disabled_rule_is_ignored(conn):
    conn.execute(
        "INSERT INTO tag_rules (tag_id, query, enabled) VALUES (%s, 'myrsky', false)",
        (tag_id(conn, "weather"),),
    )
    article = add_article(conn, 1, "Myrsky tuli Suomeen")

    apply_tags(conn)

    assert tags_of(conn, article) == {}


def test_a_manual_tag_is_kept_when_a_rule_would_add_it(conn):
    conn.execute("INSERT INTO tag_rules (tag_id, query) VALUES (%s, 'myrsky')", (tag_id(conn, "weather"),))
    article = add_article(conn, 1, "Myrsky tuli Suomeen")
    conn.execute(
        "INSERT INTO article_tags (article_id, tag_id, origin) VALUES (%s, %s, 'manual')",
        (article, tag_id(conn, "weather")),
    )

    apply_tags(conn)

    assert tags_of(conn, article) == {"weather": "manual"}
