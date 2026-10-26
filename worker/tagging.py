"""Stage 5, part 1: rule-based tags, with no LLM involved.

Feed categories map to tags through feed_category_map. Keyword rules match
articles through the Finnish full-text index. Each article is tagged once, when
its tagged_at is still NULL, so a tag removed in the web app is not added back.
Rules created later reach older articles through the web app's 30-day backfill.
Inserts use ON CONFLICT DO NOTHING, so a manual tag is never replaced.
"""

from dataclasses import dataclass

import psycopg

# The work list is read once, so an article stored mid-run is tagged next time
# instead of being marked done untagged.
PENDING_SQL = "SELECT id FROM articles WHERE tagged_at IS NULL"

FEED_TAGS_SQL = """
INSERT INTO article_tags (article_id, tag_id, origin)
SELECT DISTINCT a.id, m.tag_id, 'feed'::tag_origin
FROM articles a
JOIN feed_category_map m ON m.source_id IS NULL OR m.source_id = a.source_id
WHERE a.id = ANY(%(ids)s)
  AND m.category = ANY(a.feed_categories)
ON CONFLICT (article_id, tag_id) DO NOTHING
"""

RULE_TAGS_SQL = """
INSERT INTO article_tags (article_id, tag_id, origin)
SELECT a.id, r.tag_id, 'rule'::tag_origin
FROM tag_rules r
JOIN articles a ON a.search_tsv @@ websearch_to_tsquery('finnish', r.query)
WHERE r.enabled
  AND a.id = ANY(%(ids)s)
ON CONFLICT (article_id, tag_id) DO NOTHING
"""

MARK_SQL = "UPDATE articles SET tagged_at = now() WHERE id = ANY(%(ids)s)"


@dataclass
class TagSummary:
    articles: int = 0
    feed: int = 0
    rules: int = 0


def apply_tags(conn: psycopg.Connection) -> TagSummary:
    """Add feed-category and keyword-rule tags to untagged articles. Returns the counts."""
    ids = [row[0] for row in conn.execute(PENDING_SQL).fetchall()]
    params = {"ids": ids}
    summary = TagSummary(
        articles=len(ids),
        feed=conn.execute(FEED_TAGS_SQL, params).rowcount,
        rules=conn.execute(RULE_TAGS_SQL, params).rowcount,
    )
    conn.execute(MARK_SQL, params)
    conn.commit()
    return summary
