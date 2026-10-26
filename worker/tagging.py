"""Stage 5, part 1: rule-based tags, with no LLM involved.

Feed categories map to tags through feed_category_map. Keyword rules match
articles through the Finnish full-text index. Both run over every article on
each pass. Inserts use ON CONFLICT DO NOTHING, so a manual tag is never replaced.
"""

from dataclasses import dataclass

import psycopg

FEED_TAGS_SQL = """
INSERT INTO article_tags (article_id, tag_id, origin)
SELECT DISTINCT a.id, m.tag_id, 'feed'::tag_origin
FROM articles a
JOIN feed_category_map m ON m.source_id IS NULL OR m.source_id = a.source_id
WHERE m.category = ANY(a.feed_categories)
ON CONFLICT (article_id, tag_id) DO NOTHING
"""

RULE_TAGS_SQL = """
INSERT INTO article_tags (article_id, tag_id, origin)
SELECT a.id, r.tag_id, 'rule'::tag_origin
FROM tag_rules r
JOIN articles a ON a.search_tsv @@ websearch_to_tsquery('finnish', r.query)
WHERE r.enabled
ON CONFLICT (article_id, tag_id) DO NOTHING
"""


@dataclass
class TagSummary:
    feed: int = 0
    rules: int = 0


def apply_tags(conn: psycopg.Connection) -> TagSummary:
    """Add feed-category and keyword-rule tags. Returns how many tags were new."""
    summary = TagSummary(
        feed=conn.execute(FEED_TAGS_SQL).rowcount,
        rules=conn.execute(RULE_TAGS_SQL).rowcount,
    )
    conn.commit()
    return summary
