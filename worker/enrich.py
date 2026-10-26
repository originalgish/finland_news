"""Stage 5, part 2: English text and LLM tags, cluster headlines, same-event checks.

Articles without enriched_at go to Claude in batches of 20. Each batch returns an
English title, an English summary and up to three tags, through one forced tool
call. Replies are validated: unknown tags are dropped, ids that were not sent are
ignored, and an article missing from a reply is retried alone once. LLM tags
replace only earlier LLM tags, so manual and rule tags are never touched.

Clusters without a headline get one English headline from their enriched titles.
The same-event check used by clustering also goes through Claude.
"""

import json
from dataclasses import dataclass
from typing import Any

import psycopg

from worker.cluster import SameEvent
from worker.llm import Claude

BATCH_SIZE = 20
MAX_TAGS = 3
ENRICH_MAX_TOKENS = 4096
HEADLINE_BATCH_SIZE = 20
HEADLINE_MAX_TOKENS = 2048
HEADLINE_TITLES = 8
SAME_EVENT_MAX_TOKENS = 64

PENDING_ARTICLES_SQL = """
SELECT id, title, COALESCE(summary, '')
FROM articles
WHERE enriched_at IS NULL
ORDER BY id
LIMIT %s
"""

TAGS_SQL = "SELECT name, id FROM tags WHERE llm_assignable"

WRITE_ENRICHMENT_SQL = """
UPDATE articles
SET title_en = %(title_en)s, summary_en = %(summary_en)s, enriched_at = now()
WHERE id = %(id)s
"""

DELETE_LLM_TAGS_SQL = """
DELETE FROM article_tags
WHERE article_id = %s AND origin = 'llm'::tag_origin
"""

TAGGED_ELSEWHERE_SQL = """
SELECT DISTINCT article_id FROM article_tags
WHERE article_id = ANY(%s) AND origin <> 'llm'::tag_origin
"""

INSERT_LLM_TAG_SQL = """
INSERT INTO article_tags (article_id, tag_id, origin)
VALUES (%s, %s, 'llm'::tag_origin)
ON CONFLICT (article_id, tag_id) DO NOTHING
"""

PENDING_CLUSTERS_SQL = """
SELECT c.id
FROM clusters c
WHERE c.headline IS NULL
  AND EXISTS (
      SELECT 1 FROM articles a
      WHERE a.cluster_id = c.id AND a.enriched_at IS NOT NULL
  )
ORDER BY c.id
LIMIT %s
"""

CLUSTER_TITLES_SQL = """
SELECT title_en
FROM articles
WHERE cluster_id = %s AND title_en IS NOT NULL
ORDER BY published_at DESC
LIMIT %s
"""

WRITE_HEADLINE_SQL = "UPDATE clusters SET headline = %s WHERE id = %s"

ARTICLE_TEXT_SQL = "SELECT title, COALESCE(summary, '') FROM articles WHERE id = %s"

RECENT_CLUSTER_TITLES_SQL = """
SELECT title FROM articles
WHERE cluster_id = %s
ORDER BY published_at DESC
LIMIT 5
"""


@dataclass(frozen=True)
class Enrichment:
    title_en: str
    summary_en: str | None
    tags: list[str]


@dataclass
class EnrichSummary:
    articles: int = 0
    retried: int = 0
    still_missing: int = 0
    headlines: int = 0


# What each starter tag means. The model applies a tag only when the article is mainly about it.
TAG_DEFINITIONS: dict[str, str] = {
    "business": "a specific company, or its deals, products, results or management. Not general economics.",
    "crime": (
        "a criminal act such as assault, theft, fraud, killing or arson, or a police "
        "investigation, prosecution or court case. Accidents, fires, disasters and sporting "
        "incidents are never crime, even when people die, unless a crime is described."
    ),
    "culture": "books, music, film, television, theatre, art and entertainment people.",
    "economy": "the wider economy and markets: prices, interest rates, employment, public finances, stock markets.",
    "health": "medicine, diseases, healthcare services, medicines and medical research.",
    "local": "a particular Finnish city, region or neighbourhood, including local accidents and local services.",
    "politics": "government, parties, elections, legislation and diplomacy.",
    "sports": "matches, tournaments, teams and athletes, including results and sports disciplinary decisions.",
    "tech": "technology, software, AI, science and gaming.",
    "weather": "weather, storms and temperature.",
    "world": "events in other countries and international affairs.",
}


def enrichment_system(tags: list[str]) -> str:
    guide = "\n".join(f"  {name}: {TAG_DEFINITIONS.get(name, name)}" for name in tags)
    return (
        "You prepare Finnish news articles for an English-speaking reader.\n"
        "For every article you receive, call record_enrichment once, with one entry per article id:\n"
        "- title_en: a faithful English translation of the headline.\n"
        "- summary_en: one or two English sentences translating the given summary, based only on "
        "that text. Translate every summary, even a very short one. Leave it empty only when the "
        "article has no summary.\n"
        "- tags: at least one and at most three tags from the list below. Every article gets at "
        "least one tag: choose the closest one even when no tag fits exactly. Add a second or "
        "third tag only when the article is mainly about that subject too. If nothing fits "
        "closely, use culture for puzzles, quizzes, horoscopes and lifestyle items, local for "
        "other Finnish news, and world for other foreign news.\n"
        f"Tags:\n{guide}\n"
        "Do not add facts that are not in the article."
    )


def enrichment_tool(tags: list[str]) -> dict[str, Any]:
    tag_schema: dict[str, Any] = {"type": "string"}
    if tags:
        tag_schema["enum"] = tags
    return {
        "name": "record_enrichment",
        "description": "Record the English title, English summary and tags of each article.",
        "input_schema": {
            "type": "object",
            "properties": {
                "articles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "title_en": {"type": "string"},
                            "summary_en": {"type": "string"},
                            "tags": {"type": "array", "items": tag_schema, "maxItems": MAX_TAGS},
                        },
                        "required": ["id", "title_en", "summary_en", "tags"],
                    },
                }
            },
            "required": ["articles"],
        },
    }


def parse_enrichment(
    reply: dict[str, Any],
    sent_ids: set[int],
    tags: set[str],
    needs_summary: set[int],
    tagged_elsewhere: set[int],
) -> dict[int, Enrichment]:
    """Keep only well-formed entries for ids that were sent, with known tags.

    An entry is rejected, so the article is retried alone, when it leaves out the English
    summary of an article that has one, or when it gives no tag to an article that has no
    other tag. Rejected articles are not written.
    """
    found: dict[int, Enrichment] = {}
    for item in reply.get("articles") or []:
        if not isinstance(item, dict):
            continue
        article_id = item.get("id")
        if not isinstance(article_id, int) or article_id not in sent_ids or article_id in found:
            continue
        title_en = str(item.get("title_en") or "").strip()
        if not title_en:
            continue
        summary_en = str(item.get("summary_en") or "").strip() or None
        if article_id in needs_summary and summary_en is None:
            continue
        chosen: list[str] = []
        for tag in item.get("tags") or []:
            if isinstance(tag, str) and tag in tags and tag not in chosen:
                chosen.append(tag)
        if not chosen and article_id not in tagged_elsewhere:
            continue
        found[article_id] = Enrichment(title_en, summary_en, chosen[:MAX_TAGS])
    return found


def _enrich_batch(
    conn: psycopg.Connection,
    llm: Claude,
    rows: list[tuple[int, str, str]],
    tag_ids: dict[str, int],
    system: str,
    tool: dict[str, Any],
) -> set[int]:
    payload = [{"id": article_id, "title": title, "summary": summary} for article_id, title, summary in rows]
    reply = llm.call_tool(
        system=system,
        user=json.dumps(payload, ensure_ascii=False),
        tool=tool,
        max_tokens=ENRICH_MAX_TOKENS,
    )
    ids = [row[0] for row in rows]
    tagged_elsewhere = {row[0] for row in conn.execute(TAGGED_ELSEWHERE_SQL, (ids,)).fetchall()}
    found = parse_enrichment(
        reply,
        set(ids),
        set(tag_ids),
        needs_summary={row[0] for row in rows if row[2]},
        tagged_elsewhere=tagged_elsewhere,
    )
    for article_id, enrichment in found.items():
        conn.execute(
            WRITE_ENRICHMENT_SQL,
            {"id": article_id, "title_en": enrichment.title_en, "summary_en": enrichment.summary_en},
        )
        conn.execute(DELETE_LLM_TAGS_SQL, (article_id,))
        for tag in enrichment.tags:
            conn.execute(INSERT_LLM_TAG_SQL, (article_id, tag_ids[tag]))
    return set(found)


def enrich_articles(conn: psycopg.Connection, llm: Claude, max_articles: int) -> EnrichSummary:
    """Translate and tag up to max_articles articles. Returns counts for the run."""
    tag_ids = dict(conn.execute(TAGS_SQL).fetchall())
    names = sorted(tag_ids)
    system = enrichment_system(names)
    tool = enrichment_tool(names)

    # Read the work list once. A retried article that still fails must not be picked up again.
    pending = conn.execute(PENDING_ARTICLES_SQL, (max_articles,)).fetchall()
    summary = EnrichSummary()
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        done = _enrich_batch(conn, llm, batch, tag_ids, system, tool)
        summary.articles += len(done)
        for row in batch:
            if row[0] in done:
                continue
            summary.retried += 1
            if _enrich_batch(conn, llm, [row], tag_ids, system, tool):
                summary.articles += 1
            else:
                summary.still_missing += 1
        conn.commit()
    return summary


HEADLINE_SYSTEM = (
    "You write one neutral English headline for each news event. You receive the English "
    "headlines of the articles in each cluster. Describe the event factually, in at most "
    "twelve words. Call record_headlines once, with one entry per cluster id."
)

HEADLINE_TOOL: dict[str, Any] = {
    "name": "record_headlines",
    "description": "Record one English headline for each cluster.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clusters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "headline": {"type": "string"},
                    },
                    "required": ["id", "headline"],
                },
            }
        },
        "required": ["clusters"],
    },
}


def parse_headlines(reply: dict[str, Any], sent_ids: set[int]) -> dict[int, str]:
    found: dict[int, str] = {}
    for item in reply.get("clusters") or []:
        if not isinstance(item, dict):
            continue
        cluster_id = item.get("id")
        if not isinstance(cluster_id, int) or cluster_id not in sent_ids or cluster_id in found:
            continue
        headline = str(item.get("headline") or "").strip()
        if headline:
            found[cluster_id] = headline
    return found


def write_cluster_headlines(conn: psycopg.Connection, llm: Claude, max_clusters: int) -> int:
    cluster_ids = [row[0] for row in conn.execute(PENDING_CLUSTERS_SQL, (max_clusters,)).fetchall()]
    written = 0
    for start in range(0, len(cluster_ids), HEADLINE_BATCH_SIZE):
        batch = cluster_ids[start : start + HEADLINE_BATCH_SIZE]
        payload = [
            {
                "id": cluster_id,
                "titles": [
                    title
                    for (title,) in conn.execute(
                        CLUSTER_TITLES_SQL, (cluster_id, HEADLINE_TITLES)
                    ).fetchall()
                ],
            }
            for cluster_id in batch
        ]
        reply = llm.call_tool(
            system=HEADLINE_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
            tool=HEADLINE_TOOL,
            max_tokens=HEADLINE_MAX_TOKENS,
        )
        for cluster_id, headline in parse_headlines(reply, set(batch)).items():
            conn.execute(WRITE_HEADLINE_SQL, (headline, cluster_id))
            written += 1
        conn.commit()
    return written


SAME_EVENT_SYSTEM = (
    "You decide whether a new article and an existing news cluster report the same event: "
    "the same specific incident, announcement, match or decision. Sharing a topic, a person "
    "or a country is not enough. Call record_same_event once."
)

SAME_EVENT_TOOL: dict[str, Any] = {
    "name": "record_same_event",
    "description": "Answer whether the article and the cluster report the same event.",
    "input_schema": {
        "type": "object",
        "properties": {"same_event": {"type": "boolean"}},
        "required": ["same_event"],
    },
}


def make_same_event_judge(conn: psycopg.Connection, llm: Claude) -> SameEvent:
    """A same-event check for clustering. Answers only a literal true counts as yes."""

    def judge(article_id: int, cluster_id: int) -> bool:
        title, summary = conn.execute(ARTICLE_TEXT_SQL, (article_id,)).fetchone()
        cluster_titles = [
            row[0] for row in conn.execute(RECENT_CLUSTER_TITLES_SQL, (cluster_id,)).fetchall()
        ]
        user = (
            f"New article:\n{title}\n{summary}\n\n"
            "Existing cluster, recent headlines:\n" + "\n".join(f"- {t}" for t in cluster_titles)
        )
        reply = llm.call_tool(
            system=SAME_EVENT_SYSTEM,
            user=user,
            tool=SAME_EVENT_TOOL,
            max_tokens=SAME_EVENT_MAX_TOKENS,
        )
        return reply.get("same_event") is True

    return judge


def run_enrich(conn: psycopg.Connection, llm: Claude, max_articles: int) -> EnrichSummary:
    summary = enrich_articles(conn, llm, max_articles)
    summary.headlines = write_cluster_headlines(conn, llm, max_articles)
    return summary
