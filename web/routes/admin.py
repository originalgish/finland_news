"""Hidden admin page for checking clusters. It is not linked from the nav.

Recent stories with more than one article are listed with each member's
similarity to the cluster centroid, lowest first, so wrong merges stand out.
An article that does not belong can be split out into a cluster of its own.
"""

from flask import Blueprint, abort, g, render_template

from web import queries
from web.helpers import notice, require_htmx

bp = Blueprint("admin", __name__)

# The split article becomes a one-article cluster, dated by the article itself so it
# does not jump to the top of the feed. clustered_at stays set, so the worker leaves it alone.
SPLIT_SQL = """
WITH fresh AS (
    INSERT INTO clusters (first_seen, last_updated, article_count, source_count, centroid)
    SELECT published_at, published_at, 1, 1, embedding FROM articles WHERE id = %(id)s
    RETURNING id
)
UPDATE articles SET cluster_id = (SELECT id FROM fresh), clustered_at = now()
WHERE id = %(id)s
"""

# Recount what is left. The centroid is the mean of the remaining members, which is
# what the worker's running mean gives. The headline is written again for the new mix.
RECOUNT_SQL = """
UPDATE clusters c
SET article_count = m.articles,
    source_count = m.sources,
    centroid = m.centroid,
    headline = NULL
FROM (
    SELECT count(*) AS articles, count(DISTINCT source_id) AS sources, avg(embedding) AS centroid
    FROM articles WHERE cluster_id = %(id)s
) m
WHERE c.id = %(id)s
"""


@bp.get("/admin/clusters")
def clusters():
    return render_template("admin_clusters.html", clusters=queries.admin_clusters(g.db))


@bp.post("/admin/articles/<int:article_id>/split")
@require_htmx
def split(article_id: int):
    row = g.db.execute("SELECT cluster_id FROM articles WHERE id = %s", (article_id,)).fetchone()
    if row is None or row["cluster_id"] is None:
        abort(404)
    old_id = row["cluster_id"]
    members = g.db.execute(
        "SELECT count(*) FROM articles WHERE cluster_id = %s", (old_id,)
    ).fetchone()["count"]
    if members < 2:
        return notice("This article is already a story of its own.", ok=False)

    g.db.execute(SPLIT_SQL, {"id": article_id})
    g.db.execute(RECOUNT_SQL, {"id": old_id})
    return render_template("partials/admin_cluster.html", cluster=queries.admin_cluster(g.db, old_id))
