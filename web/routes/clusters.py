from flask import Blueprint, abort, g, render_template

from web import queries
from web.helpers import is_htmx

bp = Blueprint("clusters", __name__)


@bp.get("/clusters/<int:cluster_id>")
def detail(cluster_id: int):
    cluster = queries.cluster(g.db, cluster_id)
    if cluster is None:
        abort(404)
    context = {
        "cluster": cluster,
        "groups": queries.cluster_members(g.db, cluster_id),
        "tag_options": queries.tags(g.db),
    }
    # A card expands by asking for the body only. A direct visit gets the whole page.
    if is_htmx():
        return render_template("partials/cluster_body.html", **context)
    return render_template("cluster.html", **context)
