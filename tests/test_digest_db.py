import json
from datetime import UTC, date, datetime, timedelta

import pytest
from fake_llm import FakeLLM

from app.migrate import migrate
from worker.digest import run_digest
from worker.llm import LLMError

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)


@pytest.fixture
def conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def story(conn, key, outlets, hours_old=1, tag=None):
    """A cluster with one article per outlet, last updated hours_old hours before NOW."""
    last_updated = NOW - timedelta(hours=hours_old)
    cluster_id = conn.execute(
        """
        INSERT INTO clusters (headline, first_seen, last_updated, article_count, source_count)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (key, last_updated, last_updated, len(outlets), len(set(outlets))),
    ).fetchone()[0]
    for index, outlet in enumerate(outlets):
        source_id = conn.execute("SELECT id FROM sources WHERE name = %s", (outlet,)).fetchone()[0]
        url = f"https://example.fi/{key}/{index}"
        article_id = conn.execute(
            """
            INSERT INTO articles (source_id, url, url_canonical, title, title_en,
                                  summary, summary_en, published_at, cluster_id)
            VALUES (%s, %s, %s, %s, %s, 'Tiivistelmä', 'Summary', %s, %s) RETURNING id
            """,
            (source_id, url, url, f"{key} FI {index}", f"{key} EN {index}", last_updated, cluster_id),
        ).fetchone()[0]
        if tag and index == 0:
            tag_id = conn.execute("SELECT id FROM tags WHERE name = %s", (tag,)).fetchone()[0]
            conn.execute(
                "INSERT INTO article_tags (article_id, tag_id, origin) VALUES (%s, %s, 'feed')",
                (article_id, tag_id),
            )
    conn.commit()
    return cluster_id


def answering(prefix):
    """Writes a headline and blurb for every story in the request."""

    def respond(tool_name, user):
        assert tool_name == "record_digest"
        return {
            "stories": [
                {"id": story["id"], "headline": f"{prefix} {story['id']}", "blurb": f"Blurb {story['id']}"}
                for story in json.loads(user)
            ]
        }

    return respond


def stored(conn):
    return conn.execute("SELECT date, html, items FROM digests ORDER BY date").fetchall()


def test_top_stories_are_stored_in_ranking_order_with_english_text(conn):
    big = story(conn, "big", ["Yle", "Helsingin Sanomat", "Ilta-Sanomat"], hours_old=2)
    small = story(conn, "small", ["Yle"], hours_old=1)
    llm = FakeLLM(answering("Headline"))

    summary = run_digest(conn, llm, now=NOW)

    ((day, _, items),) = stored(conn)
    assert day == date(2026, 10, 26)
    assert [item["cluster_id"] for item in items] == [big, small]
    assert items[0]["headline"] == f"Headline {big}"
    assert len(items[0]["links"]) == 3
    sent = json.loads(llm.calls[0]["user"])
    assert sent[0]["articles"][0]["title"] == "big EN 0"
    assert (summary.stories, summary.candidates, summary.missing) == (2, 2, 0)


def test_stories_outside_the_window_are_left_out_and_claude_is_not_called(conn):
    story(conn, "old", ["Yle", "Helsingin Sanomat"], hours_old=30)
    llm = FakeLLM(answering("Headline"))

    summary = run_digest(conn, llm, now=NOW)

    assert llm.calls == []
    ((_, html, items),) = stored(conn)
    assert items == []
    assert "Nothing new in the last 24 hours." in html
    assert summary.stories == 0


def test_a_second_run_on_the_same_day_replaces_the_digest(conn):
    story(conn, "a", ["Yle"], hours_old=1)
    run_digest(conn, FakeLLM(answering("First")), now=NOW)

    run_digest(conn, FakeLLM(answering("Second")), now=NOW)

    rows = stored(conn)
    assert len(rows) == 1
    assert rows[0][2][0]["headline"].startswith("Second")


def test_a_story_without_an_answer_is_left_out_and_counted(conn):
    kept = story(conn, "kept", ["Yle", "Helsingin Sanomat"], hours_old=1)
    story(conn, "dropped", ["Yle"], hours_old=2)

    def answer_only_the_first(tool_name, user):
        first = json.loads(user)[0]
        return {"stories": [{"id": first["id"], "headline": "Only", "blurb": "One"}]}

    summary = run_digest(conn, FakeLLM(answer_only_the_first), now=NOW)

    ((_, _, items),) = stored(conn)
    assert [item["cluster_id"] for item in items] == [kept]
    assert summary.missing == 1


def test_when_no_story_gets_an_answer_the_previous_digest_is_kept(conn):
    story(conn, "a", ["Yle"], hours_old=1)
    run_digest(conn, FakeLLM(answering("Good")), now=NOW)

    with pytest.raises(LLMError):
        run_digest(conn, FakeLLM(lambda tool, user: {"stories": []}), now=NOW)

    ((_, _, items),) = stored(conn)
    assert items[0]["headline"].startswith("Good")
