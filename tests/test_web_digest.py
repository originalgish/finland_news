import xml.etree.ElementTree as ET
from datetime import date
from html import escape

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.migrate import migrate

OLD = date(2026, 10, 25)
NEW = date(2026, 10, 26)


def page(headline):
    return f'<article class="digest-item"><h2>{escape(headline)}</h2></article>'


def items(headline):
    return Jsonb(
        [
            {
                "cluster_id": 1,
                "headline": headline,
                "blurb": "Blurb",
                "links": [{"source": "Yle", "title": "Title", "url": "https://yle.fi/x"}],
            }
        ]
    )


@pytest.fixture
def stored(scratch_url):
    with psycopg.connect(scratch_url) as conn:
        migrate(conn)
        for day, headline in ((OLD, "Old story"), (NEW, "New <b>story</b>")):
            conn.execute(
                "INSERT INTO digests (date, html, items) VALUES (%s, %s, %s)",
                (day, page(headline), items(headline)),
            )
        conn.commit()


@pytest.fixture
def app(scratch_url, stored):
    from web import create_app

    application = create_app(database_url=scratch_url)
    yield application
    application.extensions["db_pool"].close()


@pytest.fixture
def client(app):
    return app.test_client()


def test_the_digest_page_shows_the_newest_day(client):
    response = client.get("/digest")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Daily digest" in body
    assert "New &lt;b&gt;story&lt;/b&gt;" in body
    assert "Old story" not in body


def test_a_day_shows_its_own_digest(client):
    body = client.get("/digest/2026-10-25").get_data(as_text=True)

    assert "Old story" in body
    assert "New &lt;b" not in body


def test_earlier_days_are_listed_for_navigation(client):
    body = client.get("/digest").get_data(as_text=True)

    assert 'href="/digest/2026-10-25"' in body


def test_an_unknown_or_malformed_day_is_not_found(client):
    assert client.get("/digest/2026-01-01").status_code == 404
    assert client.get("/digest/not-a-date").status_code == 404


def test_the_nav_links_to_the_digest(client):
    body = client.get("/digest").get_data(as_text=True)

    assert '<a href="/digest">Digest</a>' in body


def test_rss_has_one_item_per_day_newest_first(client):
    response = client.get("/digest.xml")

    assert response.status_code == 200
    assert response.mimetype == "application/rss+xml"
    root = ET.fromstring(response.data)
    titles = [item.findtext("title") for item in root.findall("./channel/item")]
    assert titles == ["Finland News digest, 2026-10-26", "Finland News digest, 2026-10-25"]


def test_rss_description_carries_the_stored_page(client):
    root = ET.fromstring(client.get("/digest.xml").data)
    first = root.find("./channel/item/description")

    assert "digest-item" in first.text
    assert "New &lt;b&gt;story" in first.text


@pytest.fixture
def empty_client(scratch_url):
    with psycopg.connect(scratch_url) as conn:
        migrate(conn)
    from web import create_app

    application = create_app(database_url=scratch_url)
    yield application.test_client()
    application.extensions["db_pool"].close()


def test_with_no_digests_the_page_says_so(empty_client):
    body = empty_client.get("/digest").get_data(as_text=True)

    assert "No digest yet." in body
