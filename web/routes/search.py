from flask import Blueprint, g, render_template, request

from web import queries
from web.helpers import is_htmx

bp = Blueprint("search", __name__)


@bp.get("/search")
def page():
    query = (request.args.get("q") or "").strip()
    results = queries.search(g.db, query) if query else []
    context = {"query": query, "results": results, "searched": bool(query)}
    # Typing in the search box swaps only the results. A direct visit gets the whole page.
    if is_htmx():
        return render_template("partials/search_results.html", **context)
    return render_template("search.html", **context)
