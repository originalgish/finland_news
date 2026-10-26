from datetime import UTC, datetime

import pytest
from fake_llm import FakeLLM, translate_all

from app.migrate import migrate
from worker.enrich import (
    enrich_articles,
    make_same_event_judge,
    run_enrich,
    write_cluster_headlines,
)

PUBLISHED = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


@pytest.fixture
def conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def add_article(conn, index, title, summary=None, source="Helsingin Sanomat", cluster_id=None):
    source_id = conn.execute("SELECT id FROM sources WHERE name = %s", (source,)).fetchone()[0]
    url = f"https://example.fi/a/{index}"
    return conn.execute(
        """
        INSERT INTO articles (source_id, url, url_canonical, title, summary, published_at, cluster_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (source_id, url, url, title, summary, PUBLISHED, cluster_id),
    ).fetchone()[0]


def tag_id(conn, name):
    return conn.execute("SELECT id FROM tags WHERE name = %s", (name,)).fetchone()[0]


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


def is_enriched(conn, article_id):
    return conn.execute("SELECT enriched_at IS NOT NULL FROM articles WHERE id = %s", (article_id,)).fetchone()[0]


def test_enrichment_writes_english_text_and_llm_tags(conn):
    article = add_article(conn, 1, "Presidentinvaalit", summary="Ehdokkaat väittelivät.")

    summary = enrich_articles(conn, FakeLLM(translate_all(("politics", "sports"))), 100)

    title_en, summary_en = conn.execute(
        "SELECT title_en, summary_en FROM articles WHERE id = %s", (article,)
    ).fetchone()
    assert (summary.articles, summary.retried, summary.still_missing) == (1, 0, 0)
    assert title_en == "EN Presidentinvaalit"
    assert summary_en == "EN Ehdokkaat väittelivät."
    assert is_enriched(conn, article)
    assert tags_of(conn, article) == {"politics": "llm", "sports": "llm"}


def test_unknown_tags_are_dropped_and_at_most_three_are_kept(conn):
    article = add_article(conn, 1, "Sää ja urheilu")
    reply = translate_all(("nonsense", "politics", "sports", "weather", "culture"))

    enrich_articles(conn, FakeLLM(reply), 100)

    assert set(tags_of(conn, article)) == {"politics", "sports", "weather"}


def test_manual_tags_are_never_replaced_or_removed(conn):
    article = add_article(conn, 1, "Kulttuuri ja politiikka")
    conn.execute(
        "INSERT INTO article_tags (article_id, tag_id, origin) VALUES (%s, %s, 'manual')",
        (article, tag_id(conn, "culture")),
    )

    # The model proposes culture too. The manual tag must keep its origin.
    enrich_articles(conn, FakeLLM(translate_all(("culture", "politics"))), 100)

    assert tags_of(conn, article) == {"culture": "manual", "politics": "llm"}


def test_reenrichment_replaces_llm_tags_but_keeps_manual_and_feed_tags(conn):
    article = add_article(conn, 1, "Vanha otsikko")
    conn.execute(
        "INSERT INTO article_tags (article_id, tag_id, origin) VALUES (%s, %s, 'manual')",
        (article, tag_id(conn, "culture")),
    )
    conn.execute(
        "INSERT INTO article_tags (article_id, tag_id, origin) VALUES (%s, %s, 'feed')",
        (article, tag_id(conn, "world")),
    )
    enrich_articles(conn, FakeLLM(translate_all(("politics",))), 100)
    conn.execute("UPDATE articles SET enriched_at = NULL, title = 'Uusi otsikko'")

    enrich_articles(conn, FakeLLM(translate_all(("sports",))), 100)

    assert tags_of(conn, article) == {"culture": "manual", "world": "feed", "sports": "llm"}


def test_batches_hold_twenty_articles(conn):
    for index in range(45):
        add_article(conn, index, f"Juttu {index}")
    llm = FakeLLM(translate_all())

    summary = enrich_articles(conn, llm, 100)

    sizes = [len(call["user"].split('"id"')) - 1 for call in llm.calls]
    assert sizes == [20, 20, 5]
    assert summary.articles == 45


def test_an_article_missing_from_a_reply_is_retried_alone(conn):
    ids = [add_article(conn, index, f"Juttu {index}") for index in range(3)]
    dropped = ids[1]
    answer = translate_all()
    seen = []

    def respond(tool_name, user):
        reply = answer(tool_name, user)
        seen.append(user)
        if len(seen) == 1:  # the first request forgets the middle article
            reply["articles"] = [a for a in reply["articles"] if a["id"] != dropped]
        return reply

    llm = FakeLLM(respond)
    summary = enrich_articles(conn, llm, 100)

    assert (summary.articles, summary.retried, summary.still_missing) == (3, 1, 0)
    assert len(llm.calls) == 2
    assert f'"id": {dropped}' in llm.calls[1]["user"]
    assert is_enriched(conn, dropped)


def test_an_article_missing_twice_stays_pending_for_the_next_run(conn):
    ids = [add_article(conn, index, f"Juttu {index}") for index in range(3)]
    dropped = ids[1]
    answer = translate_all()

    def respond(tool_name, user):
        reply = answer(tool_name, user)
        reply["articles"] = [a for a in reply["articles"] if a["id"] != dropped]
        return reply

    llm = FakeLLM(respond)
    summary = enrich_articles(conn, llm, 100)

    assert (summary.articles, summary.still_missing) == (2, 1)
    assert len(llm.calls) == 2  # the batch, then one retry; no endless loop
    assert not is_enriched(conn, dropped)


def test_ids_that_were_not_sent_are_ignored(conn):
    article = add_article(conn, 1, "Juttu")
    answer = translate_all()

    def respond(tool_name, user):
        reply = answer(tool_name, user)
        if tool_name == "record_enrichment":
            reply["articles"].append({"id": 999999, "title_en": "Ghost", "summary_en": "", "tags": []})
        return reply

    summary = enrich_articles(conn, FakeLLM(respond), 100)

    assert summary.articles == 1
    assert conn.execute("SELECT count(*) FROM articles WHERE title_en = 'Ghost'").fetchone()[0] == 0
    assert is_enriched(conn, article)


def test_an_empty_summary_is_stored_as_null(conn):
    article = add_article(conn, 1, "Pelkkä otsikko", summary=None)

    enrich_articles(conn, FakeLLM(translate_all()), 100)

    assert conn.execute("SELECT summary_en FROM articles WHERE id = %s", (article,)).fetchone()[0] is None


def test_the_cost_cap_limits_the_articles_per_run(conn):
    for index in range(12):
        add_article(conn, index, f"Juttu {index}")

    summary = enrich_articles(conn, FakeLLM(translate_all()), 5)

    assert summary.articles == 5
    assert conn.execute("SELECT count(*) FROM articles WHERE enriched_at IS NULL").fetchone()[0] == 7


def test_clusters_get_headlines_from_their_enriched_titles(conn):
    cluster = conn.execute(
        "INSERT INTO clusters (first_seen, last_updated) VALUES (now(), now()) RETURNING id"
    ).fetchone()[0]
    add_article(conn, 1, "Ensimmäinen", cluster_id=cluster)
    add_article(conn, 2, "Toinen", cluster_id=cluster)
    enrich_articles(conn, FakeLLM(translate_all()), 100)

    written = write_cluster_headlines(conn, FakeLLM(translate_all()), 100)

    assert written == 1
    assert conn.execute("SELECT headline FROM clusters WHERE id = %s", (cluster,)).fetchone()[0] == f"Headline {cluster}"


def test_clusters_without_enriched_articles_or_with_a_headline_are_skipped(conn):
    no_text = conn.execute("INSERT INTO clusters (first_seen, last_updated) VALUES (now(), now()) RETURNING id").fetchone()[0]
    add_article(conn, 1, "Ei vielä käännetty", cluster_id=no_text)
    done = conn.execute(
        "INSERT INTO clusters (headline, first_seen, last_updated) VALUES ('Kept', now(), now()) RETURNING id"
    ).fetchone()[0]
    add_article(conn, 2, "Käännetty", cluster_id=done)
    conn.execute("UPDATE articles SET enriched_at = now() WHERE title = 'Käännetty'")

    llm = FakeLLM(translate_all())
    write_cluster_headlines(conn, llm, 100)

    assert llm.calls == []  # nothing qualified, so no request was made
    assert conn.execute("SELECT headline FROM clusters WHERE id = %s", (done,)).fetchone()[0] == "Kept"


def test_run_enrich_reports_both_steps(conn):
    cluster = conn.execute("INSERT INTO clusters (first_seen, last_updated) VALUES (now(), now()) RETURNING id").fetchone()[0]
    add_article(conn, 1, "Juttu", cluster_id=cluster)

    summary = run_enrich(conn, FakeLLM(translate_all()), 100)

    assert (summary.articles, summary.headlines) == (1, 1)


def test_same_event_judge_counts_only_a_literal_true(conn):
    article = add_article(conn, 1, "Uusi juttu")
    cluster = conn.execute("INSERT INTO clusters (first_seen, last_updated) VALUES (now(), now()) RETURNING id").fetchone()[0]
    add_article(conn, 2, "Vanha juttu", cluster_id=cluster)

    def answering(value):
        return FakeLLM(lambda tool, user: {"same_event": value})

    assert make_same_event_judge(conn, answering(True))(article, cluster) is True
    assert make_same_event_judge(conn, answering(False))(article, cluster) is False
    assert make_same_event_judge(conn, answering("true"))(article, cluster) is False


def test_the_same_event_judge_sends_both_texts(conn):
    article = add_article(conn, 1, "Uusi juttu", summary="Tiivistelmä")
    cluster = conn.execute("INSERT INTO clusters (first_seen, last_updated) VALUES (now(), now()) RETURNING id").fetchone()[0]
    add_article(conn, 2, "Klusterin juttu", cluster_id=cluster)
    llm = FakeLLM(lambda tool, user: {"same_event": False})

    make_same_event_judge(conn, llm)(article, cluster)

    assert "Uusi juttu" in llm.calls[0]["user"]
    assert "Klusterin juttu" in llm.calls[0]["user"]


def test_a_summary_left_untranslated_is_retried_alone(conn):
    article = add_article(conn, 1, "Juttu", summary="Lyhyt.")
    answer = translate_all()
    seen = []

    def respond(tool_name, user):
        reply = answer(tool_name, user)
        seen.append(user)
        if len(seen) == 1:  # the first reply leaves the summary out
            for item in reply["articles"]:
                item["summary_en"] = ""
        return reply

    llm = FakeLLM(respond)
    summary = enrich_articles(conn, llm, 100)

    assert (summary.articles, summary.retried, summary.still_missing) == (1, 1, 0)
    assert conn.execute("SELECT summary_en FROM articles WHERE id = %s", (article,)).fetchone()[0] == "EN Lyhyt."


def test_an_article_left_without_any_tag_is_retried_alone(conn):
    article = add_article(conn, 1, "Ristikko")
    answer = translate_all()
    seen = []

    def respond(tool_name, user):
        reply = answer(tool_name, user)
        seen.append(user)
        if len(seen) == 1:  # the first reply gives no tag
            for item in reply["articles"]:
                item["tags"] = []
        return reply

    summary = enrich_articles(conn, FakeLLM(respond), 100)

    assert (summary.articles, summary.retried) == (1, 1)
    assert set(tags_of(conn, article)) == {"politics"}


def test_no_llm_tag_is_accepted_when_the_article_already_has_a_feed_tag(conn):
    article = add_article(conn, 1, "Urheilu")
    conn.execute(
        "INSERT INTO article_tags (article_id, tag_id, origin) VALUES (%s, %s, 'feed')",
        (article, tag_id(conn, "sports")),
    )
    llm = FakeLLM(translate_all(()))

    summary = enrich_articles(conn, llm, 100)

    assert (summary.articles, summary.retried) == (1, 0)
    assert tags_of(conn, article) == {"sports": "feed"}
