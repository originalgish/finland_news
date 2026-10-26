from datetime import UTC, date, datetime, timedelta

import pytest

from worker.digest import (
    DIGEST_SIZE,
    RECENCY_HALF_LIFE_HOURS,
    RECENCY_WEIGHT,
    TAG_WEIGHT,
    Candidate,
    build_items,
    digest_day,
    parse_digest,
    pick_stories,
    render_digest,
    score_story,
)

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)


def candidate(cluster_id, sources=1, hours_old=0.0, tag_weight=1.0):
    return Candidate(cluster_id, NOW - timedelta(hours=hours_old), sources, tag_weight)


def test_more_outlets_score_higher():
    assert score_story(candidate(1, sources=3), NOW) > score_story(candidate(2, sources=2), NOW)


def test_recency_halves_every_half_life():
    fresh = score_story(candidate(1), NOW)
    old = score_story(candidate(1, hours_old=RECENCY_HALF_LIFE_HOURS), NOW)

    assert fresh - old == pytest.approx(RECENCY_WEIGHT * 0.5)


def test_a_heavier_tag_raises_the_score():
    heavy = score_story(candidate(1, tag_weight=1.5), NOW)

    assert heavy - score_story(candidate(1), NOW) == pytest.approx(TAG_WEIGHT * 0.5)


def test_an_update_stamped_in_the_future_counts_as_fresh():
    future = Candidate(1, NOW + timedelta(hours=2), 1, 1.0)

    assert score_story(future, NOW) == score_story(candidate(1), NOW)


def test_the_best_stories_come_first_and_the_list_is_capped():
    rows = [candidate(i, sources=i) for i in range(1, 13)]

    assert pick_stories(rows, NOW) == list(range(12, 12 - DIGEST_SIZE, -1))


def test_ties_go_to_the_newer_cluster():
    assert pick_stories([candidate(3), candidate(7)], NOW) == [7, 3]


def test_the_digest_day_is_the_helsinki_day():
    late_utc = datetime(2026, 6, 1, 22, 30, tzinfo=UTC)  # 01:30 on 2 June in Helsinki

    assert digest_day(late_utc) == date(2026, 6, 2)


def test_parse_keeps_answers_for_sent_stories_only():
    reply = {
        "stories": [
            {"id": 1, "headline": " Storm hits ", "blurb": "Winds rose."},
            {"id": 2, "headline": "", "blurb": "No headline, so dropped"},
            {"id": 3, "headline": "Never sent", "blurb": "Dropped"},
            {"id": 1, "headline": "Duplicate", "blurb": "The first answer wins"},
            {"id": 4, "headline": "No blurb", "blurb": "   "},
            "not an object",
        ]
    }

    assert parse_digest(reply, {1, 2, 4}) == {1: ("Storm hits", "Winds rose.")}


def test_items_keep_ranking_order_and_only_web_links():
    members = {
        1: [
            {"title": "Storm", "summary": "", "url": "https://yle.fi/a", "source": "Yle"},
            {"title": "Odd link", "summary": "", "url": "javascript:alert(1)", "source": "HS"},
        ],
        2: [],
    }
    written = {1: ("Storm", "Winds rose."), 2: ("Budget", "Talks went on.")}

    items = build_items([2, 1, 3], written, members)

    assert [item["cluster_id"] for item in items] == [2, 1]
    assert items[1]["links"] == [{"source": "Yle", "title": "Storm", "url": "https://yle.fi/a"}]


def test_the_page_escapes_model_text():
    items = [
        {"cluster_id": 1, "headline": "<script>x</script>", "blurb": "Tom & Jerry", "links": []}
    ]

    html = render_digest(items)

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Tom &amp; Jerry" in html


def test_an_empty_digest_says_so():
    assert "Nothing new in the last 24 hours." in render_digest([])
