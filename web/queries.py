"""Read-side SQL for the web app. Writes live in the route modules."""

from datetime import datetime
from typing import Any

import psycopg

FEED_SQL = """
SELECT c.id, c.headline, c.last_updated, c.article_count, c.source_count
FROM clusters c
WHERE EXISTS (
    SELECT 1
    FROM articles a
    LEFT JOIN article_tags at ON at.article_id = a.id
    WHERE a.cluster_id = c.id
      AND (cardinality(%(sources)s::int[]) = 0 OR a.source_id = ANY(%(sources)s::int[]))
      AND (cardinality(%(tags)s::int[]) = 0 OR at.tag_id = ANY(%(tags)s::int[]))
)
  AND (
      %(before_ts)s::timestamptz IS NULL
      OR (c.last_updated, c.id) < (%(before_ts)s::timestamptz, %(before_id)s::bigint)
  )
ORDER BY c.last_updated DESC, c.id DESC
LIMIT %(limit)s
"""

LEAD_TITLES_SQL = """
SELECT DISTINCT ON (a.cluster_id) a.cluster_id, a.title, a.title_en
FROM articles a
WHERE a.cluster_id = ANY(%s)
ORDER BY a.cluster_id, a.published_at DESC
"""

CLUSTER_SOURCES_SQL = """
SELECT DISTINCT a.cluster_id, s.name
FROM articles a JOIN sources s ON s.id = a.source_id
WHERE a.cluster_id = ANY(%s)
ORDER BY a.cluster_id, s.name
"""

CLUSTER_TAGS_SQL = """
SELECT DISTINCT a.cluster_id, t.name
FROM articles a
JOIN article_tags at ON at.article_id = a.id
JOIN tags t ON t.id = at.tag_id
WHERE a.cluster_id = ANY(%s)
ORDER BY a.cluster_id, t.name
"""

CLUSTER_SQL = """
SELECT id, headline, last_updated, article_count, source_count
FROM clusters WHERE id = %s
"""

MEMBERS_SQL = """
SELECT a.id, a.title, a.title_en, a.summary, a.summary_en, a.url, a.published_at,
       s.id AS source_id, s.name AS source_name
FROM articles a JOIN sources s ON s.id = a.source_id
WHERE a.cluster_id = %s
ORDER BY s.name, a.published_at DESC
"""

ARTICLE_TAGS_SQL = """
SELECT t.id AS tag_id, t.name, at.origin::text AS origin
FROM article_tags at JOIN tags t ON t.id = at.tag_id
WHERE at.article_id = %s
ORDER BY t.name
"""

VIEWS_SQL = """
SELECT id, name, include_tags, include_sources, is_default, position
FROM views
ORDER BY position, name
"""

TAGS_SQL = """
SELECT t.id, t.name, t.weight, t.llm_assignable, count(at.article_id) AS articles
FROM tags t
LEFT JOIN article_tags at ON at.tag_id = t.id
GROUP BY t.id
ORDER BY t.name
"""

RULES_SQL = """
SELECT id, tag_id, query, enabled FROM tag_rules ORDER BY id
"""

SOURCES_SQL = """
SELECT s.id, s.name, s.enabled, s.last_fetched_at, s.last_status,
       s.consecutive_failures, s.disabled_reason,
       (SELECT count(*) FROM articles a
         WHERE a.source_id = s.id AND a.fetched_at > now() - interval '7 days') / 7.0
       AS items_per_day
FROM sources s
ORDER BY s.id
"""

FETCH_LOG_SQL = """
SELECT started_at, outcome, status_code, items_found, items_new, error
FROM fetch_log
WHERE source_id = %s
ORDER BY started_at DESC
LIMIT 5
"""

SEARCH_SQL = """
SELECT a.id, a.cluster_id, a.title, a.title_en, a.summary, a.summary_en,
       a.published_at, a.url, s.name AS source_name,
       GREATEST(
           ts_rank(a.search_tsv, websearch_to_tsquery('finnish', %(q)s)),
           ts_rank(a.search_tsv_en, websearch_to_tsquery('english', %(q)s))
       ) AS rank
FROM articles a JOIN sources s ON s.id = a.source_id
WHERE a.search_tsv @@ websearch_to_tsquery('finnish', %(q)s)
   OR a.search_tsv_en @@ websearch_to_tsquery('english', %(q)s)
ORDER BY rank DESC, a.published_at DESC
LIMIT 50
"""

RULE_PREVIEW_SQL = """
SELECT count(*) FROM articles
WHERE published_at > now() - interval '7 days'
  AND search_tsv @@ websearch_to_tsquery('finnish', %s)
"""


def feed_page(
    conn: psycopg.Connection,
    include_sources: list[int],
    include_tags: list[int],
    before: tuple[datetime, int] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """One page of clusters, newest first. A cluster matches when one of its articles does."""
    before_ts, before_id = before if before else (None, None)
    rows = conn.execute(
        FEED_SQL,
        {
            "sources": include_sources,
            "tags": include_tags,
            "before_ts": before_ts,
            "before_id": before_id,
            "limit": limit,
        },
    ).fetchall()
    return _with_card_details(conn, rows)


def _with_card_details(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    leads = {row["cluster_id"]: row for row in conn.execute(LEAD_TITLES_SQL, (ids,)).fetchall()}
    sources: dict[int, list[str]] = {}
    for row in conn.execute(CLUSTER_SOURCES_SQL, (ids,)).fetchall():
        sources.setdefault(row["cluster_id"], []).append(row["name"])
    tags: dict[int, list[str]] = {}
    for row in conn.execute(CLUSTER_TAGS_SQL, (ids,)).fetchall():
        tags.setdefault(row["cluster_id"], []).append(row["name"])

    cards = []
    for row in rows:
        lead = leads.get(row["id"], {})
        cards.append(
            {
                **row,
                "lead_title": lead.get("title"),
                "lead_title_en": lead.get("title_en"),
                "sources": sources.get(row["id"], []),
                "tags": tags.get(row["id"], []),
            }
        )
    return cards


def cluster(conn: psycopg.Connection, cluster_id: int) -> dict[str, Any] | None:
    return conn.execute(CLUSTER_SQL, (cluster_id,)).fetchone()


def cluster_members(conn: psycopg.Connection, cluster_id: int) -> list[dict[str, Any]]:
    """Member articles, grouped by source, each with its tags."""
    members = conn.execute(MEMBERS_SQL, (cluster_id,)).fetchall()
    if not members:
        return []
    tags_by_article: dict[int, list[dict[str, Any]]] = {}
    tag_rows = conn.execute(
        """
        SELECT at.article_id, t.id AS tag_id, t.name, at.origin::text AS origin
        FROM article_tags at JOIN tags t ON t.id = at.tag_id
        WHERE at.article_id = ANY(%s)
        ORDER BY t.name
        """,
        ([m["id"] for m in members],),
    ).fetchall()
    for row in tag_rows:
        tags_by_article.setdefault(row["article_id"], []).append(row)

    groups: dict[str, dict[str, Any]] = {}
    for member in members:
        group = groups.setdefault(
            member["source_name"],
            {"source_name": member["source_name"], "articles": []},
        )
        group["articles"].append({**member, "tags": tags_by_article.get(member["id"], [])})
    return list(groups.values())


def article_tags(conn: psycopg.Connection, article_id: int) -> list[dict[str, Any]]:
    return conn.execute(ARTICLE_TAGS_SQL, (article_id,)).fetchall()


def views(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(VIEWS_SQL).fetchall()


def tags(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(TAGS_SQL).fetchall()


def rules_by_tag(conn: psycopg.Connection) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(RULES_SQL).fetchall():
        grouped.setdefault(row["tag_id"], []).append(row)
    return grouped


def sources(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(SOURCES_SQL).fetchall()


def fetch_log(conn: psycopg.Connection, source_id: int) -> list[dict[str, Any]]:
    return conn.execute(FETCH_LOG_SQL, (source_id,)).fetchall()


def search(conn: psycopg.Connection, query: str) -> list[dict[str, Any]]:
    return conn.execute(SEARCH_SQL, {"q": query}).fetchall()


def rule_preview_count(conn: psycopg.Connection, query: str) -> int:
    return conn.execute(RULE_PREVIEW_SQL, (query,)).fetchone()["count"]
