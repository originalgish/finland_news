from datetime import UTC, datetime
from pathlib import Path

import feedparser
import pytest

from worker.normalize import (
    SUMMARY_LIMIT,
    canonical_url,
    clean_summary,
    clean_text,
    normalize_entries,
    parse_published,
)

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
FETCHED = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://yle.fi/a/74-20253214?origin=rss", "https://yle.fi/a/74-20253214"),
        (
            "HTTPS://WWW.Iltalehti.fi/kotimaa/a/abc/?utm_source=rss&utm_medium=x&b=2&a=1#frag",
            "https://www.iltalehti.fi/kotimaa/a/abc?a=1&b=2",
        ),
        ("https://www.example.fi/amp/uutinen", "https://www.example.fi/uutinen"),
        ("https://www.example.fi/uutinen/amp", "https://www.example.fi/uutinen"),
        ("https://www.example.fi/uutinen?amp", "https://www.example.fi/uutinen"),
        ("https://www.hs.fi/", "https://www.hs.fi"),
        ("https://www.hs.fi/x/?fbclid=abc&keep=1", "https://www.hs.fi/x?keep=1"),
        ("https://www.hs.fi/x?gclid=1&z=9&a=1", "https://www.hs.fi/x?a=1&z=9"),
        ("https://www.hs.fi/x?UTM_Campaign=spring", "https://www.hs.fi/x"),
    ],
)
def test_canonical_url_removes_tracking_and_normalizes(url, expected):
    assert canonical_url(url) == expected


def test_canonical_url_is_stable_for_its_own_output():
    once = canonical_url("https://www.is.fi/politiikka/art-1.html?utm_source=rss&x=2")
    assert canonical_url(once) == once


def test_clean_text_decodes_entities_and_collapses_whitespace():
    assert clean_text(" 55 000  euron\n kylttiki &amp; muuta ") == "55 000 euron kylttiki & muuta"


def test_clean_text_removes_soft_hyphens_so_words_stay_whole():
    assert clean_text("vanhimman peti­kum") == "vanhimman petikum"
    assert clean_text("peti&shy;kum") == "petikum"


def test_clean_text_handles_missing_values():
    assert clean_text(None) == ""
    assert clean_text("   ") == ""


def test_clean_summary_strips_markup_and_entities():
    html = (
        "<p>Ylen&nbsp;uutinen <a href='x'>linkki</a> &amp; lisää.</p>"
        "<ul><li>kohta</li></ul>"
    )
    assert clean_summary(html) == "Ylen uutinen linkki & lisää. kohta"


def test_clean_summary_is_none_when_nothing_is_left():
    assert clean_summary(None) is None
    assert clean_summary("<p> </p>") is None


def test_clean_summary_cuts_at_a_word_boundary():
    summary = clean_summary("sana " * 300)

    assert len(summary) <= SUMMARY_LIMIT
    assert summary.endswith("sana")


def test_missing_date_falls_back_to_fetched_at():
    assert parse_published(None, None, FETCHED, FETCHED) == FETCHED


def test_aware_date_is_converted_to_utc():
    published = parse_published("Sun, 25 Oct 2026 15:54:40 +0200", None, FETCHED, LATER)

    assert published == datetime(2026, 10, 25, 13, 54, 40, tzinfo=UTC)


def test_naive_date_is_read_as_helsinki_time():
    # 15 June is summer time in Helsinki (UTC+3).
    published = parse_published("Mon, 15 Jun 2026 12:00:00", None, FETCHED, FETCHED)

    assert published == datetime(2026, 6, 15, 9, 0, tzinfo=UTC)


def test_future_date_is_clamped_to_now():
    published = parse_published("Mon, 01 Jan 2029 00:00:00 GMT", None, FETCHED, FETCHED)

    assert published == FETCHED


def test_iso_date_is_accepted():
    published = parse_published("2026-10-25T10:00:00Z", None, FETCHED, FETCHED)

    assert published == datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


def test_unreadable_string_uses_feedparser_struct_when_available():
    parsed = datetime(2026, 10, 25, 14, 0, tzinfo=UTC).timetuple()

    published = parse_published("not a date", parsed, FETCHED, LATER)

    assert published == datetime(2026, 10, 25, 14, 0, tzinfo=UTC)


def _entry(**fields):
    return fields


def test_entries_without_link_or_title_are_skipped():
    entries = [
        _entry(link="", title="No link"),
        _entry(link="https://www.hs.fi/a.html", title="   "),
        _entry(link="https://www.hs.fi/b.html", title="Kept"),
    ]

    articles = normalize_entries(entries, FETCHED, FETCHED)

    assert [a.title for a in articles] == ["Kept"]


def test_categories_are_deduplicated_and_keep_order():
    entry = _entry(
        link="https://www.hs.fi/a.html",
        title="Otsikko",
        tags=[{"term": "Urheilu"}, {"term": " Pelit "}, {"term": "Urheilu"}, {"term": ""}],
    )

    (article,) = normalize_entries([entry], FETCHED, FETCHED)

    assert article.feed_categories == ["Urheilu", "Pelit"]


def test_guid_equal_to_another_items_link_is_dropped():
    entries = [
        _entry(id="https://www.hs.fi/b.html", link="https://www.hs.fi/a.html", title="A"),
        _entry(id="https://www.hs.fi/b.html", link="https://www.hs.fi/b.html", title="B"),
    ]

    first, second = normalize_entries(entries, FETCHED, FETCHED)

    assert first.guid is None
    assert second.guid == "https://www.hs.fi/b.html"


def test_guid_equal_to_own_link_is_kept():
    entry = _entry(id="https://yle.fi/a/1", link="https://yle.fi/a/1?origin=rss", title="A")

    (article,) = normalize_entries([entry], FETCHED, FETCHED)

    assert article.guid == "https://yle.fi/a/1"
    assert article.url_canonical == "https://yle.fi/a/1"


@pytest.mark.parametrize(
    "filename, expected_count",
    [
        ("yle.xml", 10),
        ("helsingin_sanomat.xml", 99),
        ("ilta_sanomat.xml", 100),
        ("iltalehti.xml", 20),
        ("kauppalehti.xml", 10),
    ],
)
def test_fixture_feeds_normalize_cleanly(filename, expected_count):
    entries = feedparser.parse((FIXTURES / filename).read_bytes()).entries

    articles = normalize_entries(entries, FETCHED, FETCHED)

    assert len(articles) == expected_count
    assert all(a.title and a.url_canonical.startswith("https://") for a in articles)
    assert all("utm_" not in a.url_canonical and "origin=" not in a.url_canonical for a in articles)
    assert all(a.published_at <= FETCHED for a in articles)


def test_iltalehti_fixture_has_no_summaries():
    entries = feedparser.parse((FIXTURES / "iltalehti.xml").read_bytes()).entries

    articles = normalize_entries(entries, FETCHED, FETCHED)

    assert all(a.summary is None for a in articles)
