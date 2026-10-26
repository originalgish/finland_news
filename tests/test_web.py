import re
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.migrate import migrate
from web import routes

HX = {"HX-Request": "true"}
NOW = datetime.now(UTC)


@pytest.fixture
def seeded(scratch_url):
    """A migrated database with two stories: Jaro (two outlets) and a storm (Yle only)."""
    with psycopg.connect(scratch_url) as conn:
        migrate(conn)
        source = dict(conn.execute("SELECT name, id FROM sources").fetchall())
        tag = dict(conn.execute("SELECT name, id FROM tags").fetchall())
        jaro = conn.execute(
            """
            INSERT INTO clusters (headline, first_seen, last_updated, article_count, source_count)
            VALUES ('Jaro stays in the Veikkausliiga', now(), %s, 2, 2) RETURNING id
            """,
            (NOW,),
        ).fetchone()[0]
        storm = conn.execute(
            """
            INSERT INTO clusters (headline, first_seen, last_updated, article_count, source_count)
            VALUES ('Storm is coming to southern Finland', now(), %s, 1, 1) RETURNING id
            """,
            (NOW - timedelta(hours=2),),
        ).fetchone()[0]
        articles = {}
        rows = [
            ("jaro-hs", "Helsingin Sanomat", "Jaro jatkaa Veikkausliigassa", "Jaro stays in the league", jaro, NOW, ["sports"]),
            ("jaro-is", "Ilta-Sanomat", "Jaro säilytti paikkansa", "Jaro kept its place", jaro, NOW, []),
            ("storm-yle", "Yle", "Myrsky tulee", "Storm is coming", storm, NOW - timedelta(hours=2), []),
        ]
        for key, outlet, title, title_en, cluster_id, published, feed_tags in rows:
            article = conn.execute(
                """
                INSERT INTO articles (source_id, url, url_canonical, title, title_en,
                                      summary, summary_en, published_at, cluster_id)
                VALUES (%s, %s, %s, %s, %s, 'Tiivistelmä', 'Summary', %s, %s) RETURNING id
                """,
                (source[outlet], f"https://example.fi/{key}", f"https://example.fi/{key}",
                 title, title_en, published, cluster_id),
            ).fetchone()[0]
            articles[key] = article
            for name in feed_tags:
                conn.execute(
                    "INSERT INTO article_tags (article_id, tag_id, origin) VALUES (%s, %s, 'feed')",
                    (article, tag[name]),
                )
        conn.execute(
            "INSERT INTO article_tags (article_id, tag_id, origin) VALUES (%s, %s, 'llm')",
            (articles["storm-yle"], tag["weather"]),
        )
        conn.commit()
    return {"source": source, "tag": tag, "jaro": jaro, "storm": storm, "articles": articles}


@pytest.fixture
def app(scratch_url, seeded):
    from web import create_app

    application = create_app(database_url=scratch_url)
    yield application
    application.extensions["db_pool"].close()


@pytest.fixture
def client(app):
    return app.test_client()


def db_rows(url, sql, params=()):
    with psycopg.connect(url) as conn:
        return conn.execute(sql, params).fetchall()


def test_healthz_checks_the_database(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"


def test_the_feed_lists_the_newest_stories_first(client, seeded):
    html = client.get("/").get_data(as_text=True)

    assert html.index("Jaro stays in the Veikkausliiga") < html.index("Storm is coming to southern Finland")


def test_the_language_cookie_chooses_english_or_finnish_titles(client):
    english = client.get("/").get_data(as_text=True)
    client.set_cookie("lang", "fi")
    finnish = client.get("/").get_data(as_text=True)

    assert "Jaro stays in the Veikkausliiga" in english
    assert "Jaro jatkaa Veikkausliigassa" in finnish
    assert "Jaro stays in the Veikkausliiga" not in finnish


def test_a_view_limits_the_feed_to_its_outlets(client, seeded):
    response = client.post(
        "/views",
        data={"name": "Yle only", "include_sources": [str(seeded["source"]["Yle"])]},
        headers=HX,
    )
    assert response.status_code == 200
    view_id = db_rows(client.application.extensions["db_pool"].conninfo, "SELECT id FROM views WHERE name = 'Yle only'")[0][0]

    html = client.get(f"/?view={view_id}").get_data(as_text=True)

    assert "Storm is coming to southern Finland" in html
    assert "Jaro stays in the Veikkausliiga" not in html


def test_a_view_matches_a_story_when_one_of_its_articles_matches(client, seeded):
    client.post(
        "/views",
        data={"name": "Sports", "include_tags": [str(seeded["tag"]["sports"])]},
        headers=HX,
    )
    view_id = db_rows(client.application.extensions["db_pool"].conninfo, "SELECT id FROM views WHERE name = 'Sports'")[0][0]

    html = client.get(f"/?view={view_id}").get_data(as_text=True)

    assert "Jaro stays in the Veikkausliiga" in html
    assert "Storm is coming" not in html


def test_the_default_view_opens_when_no_view_is_chosen(client, seeded):
    client.post(
        "/views",
        data={"name": "Home", "include_sources": [str(seeded["source"]["Yle"])], "is_default": "on"},
        headers=HX,
    )

    html = client.get("/").get_data(as_text=True)

    assert "Storm is coming" in html
    assert "Jaro stays" not in html
    assert "Jaro stays" in client.get("/?view=all").get_data(as_text=True)


def test_an_htmx_feed_request_returns_only_the_fragment(client):
    html = client.get("/", headers=HX).get_data(as_text=True)

    assert "<html" not in html
    assert "Jaro stays in the Veikkausliiga" in html


def test_the_feed_pages_through_older_stories(client, monkeypatch):
    monkeypatch.setattr(routes.feed, "PAGE_SIZE", 1)

    first = client.get("/").get_data(as_text=True)
    assert "Jaro stays in the Veikkausliiga" in first
    assert "Storm is coming" not in first
    sentinel = re.search(r'hx-get="([^"]+)" hx-trigger="revealed"', first)
    assert sentinel is not None
    next_link = sentinel.group(1).replace("&amp;", "&")

    second = client.get(next_link, headers=HX).get_data(as_text=True)

    assert "Storm is coming to southern Finland" in second
    assert "<html" not in second


def test_a_cluster_page_shows_both_languages_grouped_by_outlet(client, seeded):
    html = client.get(f"/clusters/{seeded['jaro']}").get_data(as_text=True)

    assert "Jaro jatkaa Veikkausliigassa" in html
    assert "Jaro stays in the league" in html
    assert "Helsingin Sanomat" in html and "Ilta-Sanomat" in html


def test_a_cluster_fragment_is_the_article_list_only(client, seeded):
    html = client.get(f"/clusters/{seeded['jaro']}", headers=HX).get_data(as_text=True)

    assert "<html" not in html
    assert "cluster-body" in html


def test_an_unknown_cluster_is_not_found(client):
    assert client.get("/clusters/999999").status_code == 404


def test_a_manual_tag_can_be_added_and_removed(client, seeded, app):
    article = seeded["articles"]["jaro-is"]
    tag = seeded["tag"]["politics"]
    url = app.extensions["db_pool"].conninfo

    added = client.post(f"/articles/{article}/tags", data={"tag_id": str(tag)}, headers=HX)
    assert added.status_code == 200
    assert "politics" in added.get_data(as_text=True)
    assert db_rows(url, "SELECT origin::text FROM article_tags WHERE article_id = %s AND tag_id = %s", (article, tag)) == [("manual",)]

    removed = client.delete(f"/articles/{article}/tags/{tag}", headers=HX)
    assert removed.status_code == 200
    assert db_rows(url, "SELECT count(*) FROM article_tags WHERE article_id = %s AND tag_id = %s", (article, tag)) == [(0,)]


def test_adding_a_tag_turns_an_llm_tag_into_a_manual_one(client, seeded, app):
    article = seeded["articles"]["storm-yle"]
    url = app.extensions["db_pool"].conninfo

    client.post(f"/articles/{article}/tags", data={"tag_id": str(seeded["tag"]["weather"])}, headers=HX)

    assert db_rows(url, "SELECT origin::text FROM article_tags WHERE article_id = %s AND tag_id = %s", (article, seeded["tag"]["weather"])) == [("manual",)]


@pytest.mark.parametrize(
    "method, path, data",
    [
        ("post", "/tags", {"name": "x"}),
        ("post", "/views", {"name": "x"}),
        ("post", "/sources/1/toggle", {}),
        ("delete", "/rules/1", {}),
    ],
)
def test_writes_must_come_from_htmx(client, method, path, data):
    response = getattr(client, method)(path, data=data)

    assert response.status_code == 403


def test_a_keyword_rule_backfills_recent_articles_and_previews_matches(client, seeded, app):
    url = app.extensions["db_pool"].conninfo
    tag = seeded["tag"]["tech"]

    response = client.post(f"/tags/{tag}/rules", data={"query": "Jaro"}, headers=HX)

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'id="notice"' in body and "2 articles from the last 30 days match, 2 newly tagged" in body
    tagged = db_rows(url, "SELECT count(*) FROM article_tags WHERE tag_id = %s AND origin = 'rule'", (tag,))
    assert tagged == [(2,)]
    preview = client.get("/rules/preview", query_string={"query": "Jaro"}).get_data(as_text=True)
    assert "2 articles match" in preview


def test_an_empty_rule_is_refused(client, seeded):
    response = client.post(f"/tags/{seeded['tag']['tech']}/rules", data={"query": "  "}, headers=HX)

    assert "needs a search query" in response.get_data(as_text=True)


def test_a_tag_can_be_created_renamed_and_deleted(client, seeded, app):
    url = app.extensions["db_pool"].conninfo

    created = client.post("/tags", data={"name": "kulttuuri-2", "weight": "1.5", "llm_assignable": "on"}, headers=HX)
    assert created.status_code == 200
    tag_id = db_rows(url, "SELECT id FROM tags WHERE name = 'kulttuuri-2'")[0][0]

    duplicate = client.post("/tags", data={"name": "kulttuuri-2"}, headers=HX).get_data(as_text=True)
    assert "already exists" in duplicate

    updated = client.patch(f"/tags/{tag_id}", data={"weight": "2.5"}, headers=HX)
    assert "2.5" in updated.get_data(as_text=True)
    assert db_rows(url, "SELECT weight FROM tags WHERE id = %s", (tag_id,)) == [(2.5,)]

    assert client.delete(f"/tags/{tag_id}", headers=HX).get_data(as_text=True) == ""
    assert db_rows(url, "SELECT count(*) FROM tags WHERE id = %s", (tag_id,)) == [(0,)]


def test_only_one_view_can_be_the_default(client, app):
    url = app.extensions["db_pool"].conninfo
    client.post("/views", data={"name": "First", "is_default": "on"}, headers=HX)
    client.post("/views", data={"name": "Second", "is_default": "on"}, headers=HX)

    defaults = db_rows(url, "SELECT name FROM views WHERE is_default ORDER BY name")

    assert defaults == [("Second",)]


def test_a_view_can_be_renamed_and_deleted(client, app):
    url = app.extensions["db_pool"].conninfo
    client.post("/views", data={"name": "Old name"}, headers=HX)
    view_id = db_rows(url, "SELECT id FROM views WHERE name = 'Old name'")[0][0]

    client.patch(f"/views/{view_id}", data={"name": "New name", "position": "3"}, headers=HX)
    assert db_rows(url, "SELECT name, position FROM views WHERE id = %s", (view_id,)) == [("New name", 3)]

    client.delete(f"/views/{view_id}", headers=HX)
    assert db_rows(url, "SELECT count(*) FROM views WHERE id = %s", (view_id,)) == [(0,)]


def test_a_source_can_be_switched_off_and_back_on(client, seeded, app):
    url = app.extensions["db_pool"].conninfo
    source = seeded["source"]["Yle"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE sources SET consecutive_failures = 5, disabled_reason = 'boom' WHERE id = %s", (source,))

    client.post(f"/sources/{source}/toggle", headers=HX)
    assert db_rows(url, "SELECT enabled FROM sources WHERE id = %s", (source,)) == [(False,)]

    client.post(f"/sources/{source}/toggle", headers=HX)
    assert db_rows(url, "SELECT enabled, consecutive_failures, disabled_reason FROM sources WHERE id = %s", (source,)) == [(True, 0, None)]


def test_search_finds_finnish_and_english_words(client, seeded):
    finnish = client.get("/search", query_string={"q": "Myrsky"}).get_data(as_text=True)
    english = client.get("/search", query_string={"q": "Veikkausliiga"}).get_data(as_text=True)

    # The default language is English, so results show the English article title.
    assert "Storm is coming" in finnish
    assert "Jaro stays in the league" in english


def test_search_without_a_match_says_so(client):
    html = client.get("/search", query_string={"q": "zzzzqqq"}).get_data(as_text=True)

    assert "Nothing matches" in html


def test_the_language_switch_stays_on_the_same_site(client):
    response = client.get("/lang/fi", query_string={"next": "/tags"})
    assert response.status_code == 302
    assert response.headers["Location"] == "/tags"
    assert "lang=fi" in response.headers["Set-Cookie"]

    escaped = client.get("/lang/en", query_string={"next": "https://example.com/"})
    assert escaped.headers["Location"] == "/"


@pytest.mark.parametrize("path", ["/tags", "/views", "/sources", "/search?q=Jaro"])
def test_management_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_admin_clusters_lists_only_stories_with_several_articles(client):
    html = client.get("/admin/clusters").get_data(as_text=True)

    assert "Jaro stays in the Veikkausliiga" in html
    assert "Storm is coming to southern Finland" not in html
    assert html.count(">Split out</button>") == 2


def test_splitting_an_article_out_gives_it_its_own_cluster(client, seeded, scratch_url):
    article = seeded["articles"]["jaro-is"]

    response = client.post(f"/admin/articles/{article}/split", headers=HX)

    assert response.status_code == 200
    assert ">Split out</button>" not in response.get_data(as_text=True)
    left = db_rows(scratch_url, "SELECT article_count, source_count, headline FROM clusters WHERE id = %s", (seeded["jaro"],))
    assert left == [(1, 1, None)]
    moved = db_rows(
        scratch_url,
        "SELECT c.article_count, c.source_count FROM articles a JOIN clusters c ON c.id = a.cluster_id WHERE a.id = %s",
        (article,),
    )
    assert moved == [(1, 1)]


def test_a_lone_article_cannot_be_split_out(client, seeded):
    response = client.post(f"/admin/articles/{seeded['articles']['storm-yle']}/split", headers=HX)

    assert "already a story of its own" in response.get_data(as_text=True)


def test_split_needs_htmx(client, seeded):
    assert client.post(f"/admin/articles/{seeded['articles']['jaro-is']}/split").status_code == 403
