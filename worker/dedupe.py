"""Stage 2, part 2: store normalized articles without duplicates.

Articles are keyed by canonical URL. An article whose URL is new but whose feed
GUID is already stored is treated as the same article with an edited URL.
"""

from typing import Any

import psycopg

from worker.normalize import Article

UPSERT_SQL = """
INSERT INTO articles (source_id, guid, url, url_canonical, title, summary,
                      feed_categories, published_at)
VALUES (%(source_id)s, %(guid)s, %(url)s, %(url_canonical)s, %(title)s, %(summary)s,
        %(feed_categories)s, %(published_at)s)
ON CONFLICT (url_canonical) DO UPDATE
   SET title = EXCLUDED.title,
       summary = EXCLUDED.summary,
       updated_at = now(),
       -- a changed headline must be translated again
       enriched_at = CASE WHEN articles.title IS DISTINCT FROM EXCLUDED.title
                          THEN NULL ELSE articles.enriched_at END,
       -- changed text must be embedded again, or the vector goes stale
       embedded_at = CASE WHEN articles.title IS DISTINCT FROM EXCLUDED.title
                            OR articles.summary IS DISTINCT FROM EXCLUDED.summary
                          THEN NULL ELSE articles.embedded_at END
RETURNING id, (xmax = 0) AS inserted
"""

URL_EXISTS_SQL = "SELECT 1 FROM articles WHERE url_canonical = %s"

FIND_BY_GUID_SQL = "SELECT id FROM articles WHERE source_id = %s AND guid = %s"

MOVE_SQL = """
UPDATE articles
SET url = %(url)s,
    url_canonical = %(url_canonical)s,
    title = %(title)s,
    summary = %(summary)s,
    updated_at = now(),
    enriched_at = CASE WHEN articles.title IS DISTINCT FROM %(title)s
                       THEN NULL ELSE articles.enriched_at END,
    embedded_at = CASE WHEN articles.title IS DISTINCT FROM %(title)s
                         OR articles.summary IS DISTINCT FROM %(summary)s
                       THEN NULL ELSE articles.embedded_at END
WHERE id = %(id)s
"""


def store_articles(conn: psycopg.Connection, source_id: int, articles: list[Article]) -> int:
    """Upsert every article and return how many new rows were inserted."""
    return sum(upsert_article(conn, source_id, article) for article in articles)


def upsert_article(conn: psycopg.Connection, source_id: int, article: Article) -> bool:
    """Insert or update one article. Returns True when a new row was inserted."""
    params: dict[str, Any] = {
        "source_id": source_id,
        "guid": article.guid,
        "url": article.url,
        "url_canonical": article.url_canonical,
        "title": article.title,
        "summary": article.summary,
        "feed_categories": article.feed_categories,
        "published_at": article.published_at,
    }

    # No row has this URL yet, but the feed's GUID is known: the URL changed.
    if article.guid and conn.execute(URL_EXISTS_SQL, (article.url_canonical,)).fetchone() is None:
        row = conn.execute(FIND_BY_GUID_SQL, (source_id, article.guid)).fetchone()
        if row is not None:
            conn.execute(MOVE_SQL, {**params, "id": row[0]})
            return False

    return conn.execute(UPSERT_SQL, params).fetchone()[1]
