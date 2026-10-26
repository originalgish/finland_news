"""Stage 4: group articles about the same event into clusters.

Articles are handled oldest first, one at a time. Each looks for its nearest
clustered neighbour from the previous 72 hours, then:

- joins that neighbour's cluster when similarity is at least MERGE_THRESHOLD,
- asks the same-event check when similarity is in the borderline band,
- otherwise starts a new cluster.

Thresholds are calibrated on hand-labelled pairs from the stored articles, using
the MiniLM embeddings (tests/fixtures/cluster_calibration.csv, 109 labelled pairs).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

import numpy as np
import psycopg

from worker.embed import to_pgvector

MERGE_THRESHOLD = 0.80  # precision 1.00 on the labelled pairs; 0.775 gave 0.92
BORDERLINE_FLOOR = 0.60  # keeps 28 of the 29 same-event pairs
WINDOW = timedelta(hours=72)
BATCH_SIZE = 200

PENDING_SQL = """
SELECT id, source_id, published_at, embedding::text
FROM articles
WHERE clustered_at IS NULL AND embedded_at IS NOT NULL
ORDER BY published_at, id
LIMIT %s
"""

# An exact search over the window. The window holds a few days of articles, so this is
# cheap. Ordering the whole table through the HNSW index instead would filter its top
# matches by date afterwards, and once older articles crowd the top matches the query
# finds no neighbour at all.
NEAREST_SQL = """
WITH recent AS MATERIALIZED (
    SELECT cluster_id, source_id, embedding
    FROM articles
    WHERE cluster_id IS NOT NULL
      AND published_at > %(since)s
)
SELECT cluster_id, source_id, 1 - (embedding <=> %(embedding)s::vector) AS similarity
FROM recent
ORDER BY embedding <=> %(embedding)s::vector
LIMIT 1
"""

# A story is dated by its articles' publication times, not by when the worker ran, so
# the feed order, "time ago" and the digest's recency follow the news itself.
NEW_CLUSTER_SQL = """
INSERT INTO clusters (first_seen, last_updated, article_count, source_count, centroid)
VALUES (%(published_at)s, %(published_at)s, 1, 1, %(centroid)s::vector)
RETURNING id
"""

CLUSTER_STATE_SQL = "SELECT centroid::text, article_count FROM clusters WHERE id = %s"

JOIN_CLUSTER_SQL = """
UPDATE clusters
SET centroid = %(centroid)s::vector,
    article_count = %(article_count)s,
    source_count = (SELECT count(DISTINCT source_id) FROM articles WHERE cluster_id = %(id)s),
    last_updated = GREATEST(last_updated, %(published_at)s),
    headline = NULL
WHERE id = %(id)s
"""

ASSIGN_SQL = """
UPDATE articles
SET cluster_id = %(cluster_id)s, clustered_at = now()
WHERE id = %(id)s
"""


class Decision(Enum):
    NEW = "new"
    JOIN = "join"
    ASK = "ask"  # borderline: needs the same-event check before deciding


@dataclass(frozen=True)
class Candidate:
    cluster_id: int
    source_id: int
    similarity: float


def decide(candidate: Candidate | None) -> Decision:
    """Decide what to do with an article, given its nearest clustered neighbour."""
    if candidate is None or candidate.similarity < BORDERLINE_FLOOR:
        return Decision.NEW
    if candidate.similarity >= MERGE_THRESHOLD:
        return Decision.JOIN
    return Decision.ASK


# Called as same_event(article_id, cluster_id) for borderline pairs. Phase 5 supplies the LLM check.
SameEvent = Callable[[int, int], bool]


@dataclass
class ClusterSummary:
    articles: int = 0
    joined: int = 0
    created: int = 0
    asked: int = 0
    asked_yes: int = 0


def cluster_pending(
    conn: psycopg.Connection,
    same_event: SameEvent | None = None,
    batch_size: int = BATCH_SIZE,
) -> ClusterSummary:
    """Assign every embedded, unclustered article to a cluster."""
    summary = ClusterSummary()
    while True:
        rows = conn.execute(PENDING_SQL, (batch_size,)).fetchall()
        if not rows:
            return summary
        for article_id, _source_id, published_at, embedding in rows:
            _cluster_one(conn, article_id, published_at, embedding, same_event, summary)
            summary.articles += 1
        conn.commit()


def _cluster_one(
    conn: psycopg.Connection,
    article_id: int,
    published_at: datetime,
    embedding: str,
    same_event: SameEvent | None,
    summary: ClusterSummary,
) -> None:
    nearest = conn.execute(
        NEAREST_SQL,
        {"embedding": embedding, "since": published_at - WINDOW},
    ).fetchone()
    candidate = Candidate(*nearest) if nearest is not None else None

    decision = decide(candidate)
    if decision is Decision.ASK:
        summary.asked += 1
        if same_event is not None and same_event(article_id, candidate.cluster_id):
            summary.asked_yes += 1
            decision = Decision.JOIN
        else:
            decision = Decision.NEW

    if decision is Decision.JOIN:
        _join(conn, article_id, candidate.cluster_id, published_at, embedding)
        summary.joined += 1
    else:
        _create(conn, article_id, published_at, embedding)
        summary.created += 1


def _create(
    conn: psycopg.Connection, article_id: int, published_at: datetime, embedding: str
) -> None:
    cluster_id = conn.execute(
        NEW_CLUSTER_SQL, {"published_at": published_at, "centroid": embedding}
    ).fetchone()[0]
    conn.execute(ASSIGN_SQL, {"id": article_id, "cluster_id": cluster_id})


def _join(
    conn: psycopg.Connection,
    article_id: int,
    cluster_id: int,
    published_at: datetime,
    embedding: str,
) -> None:
    centroid_text, count = conn.execute(CLUSTER_STATE_SQL, (cluster_id,)).fetchone()
    old = np.array(centroid_text.strip("[]").split(","), dtype=np.float64)
    new = np.array(embedding.strip("[]").split(","), dtype=np.float64)
    # Running mean of the member vectors.
    centroid = (old * count + new) / (count + 1)

    conn.execute(ASSIGN_SQL, {"id": article_id, "cluster_id": cluster_id})
    conn.execute(
        JOIN_CLUSTER_SQL,
        {
            "id": cluster_id,
            "centroid": to_pgvector(centroid),
            "article_count": count + 1,
            "published_at": published_at,
        },
    )


def run_cluster(conn: psycopg.Connection, same_event: SameEvent | None = None) -> ClusterSummary:
    return cluster_pending(conn, same_event=same_event)
