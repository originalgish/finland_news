from datetime import datetime
from typing import Any

from flask import Blueprint, abort, g, render_template, request, url_for

from web import queries
from web.helpers import is_htmx, parse_int

bp = Blueprint("feed", __name__)
PAGE_SIZE = 30


def _before() -> tuple[datetime, int] | None:
    """The keyset cursor from the infinite-scroll link: the last cluster already shown."""
    raw_ts = request.args.get("before_ts")
    cluster_id = parse_int(request.args.get("before_id"))
    if not raw_ts or cluster_id is None:
        return None
    try:
        return datetime.fromisoformat(raw_ts), cluster_id
    except ValueError:
        return None


def _active_view(all_views: list[dict[str, Any]]) -> dict[str, Any] | None:
    """?view=<id> picks a view, ?view=all shows everything, no parameter uses the default."""
    raw = request.args.get("view")
    if raw is None:
        return next((v for v in all_views if v["is_default"]), None)
    if raw == "all":
        return None
    view_id = parse_int(raw)
    view = next((v for v in all_views if v["id"] == view_id), None)
    if view is None:
        abort(404)
    return view


@bp.get("/")
def index():
    all_views = queries.views(g.db)
    view = _active_view(all_views)
    before = _before()
    cards = queries.feed_page(
        g.db,
        include_sources=view["include_sources"] if view else [],
        include_tags=view["include_tags"] if view else [],
        before=before,
        limit=PAGE_SIZE,
    )
    more = len(cards) == PAGE_SIZE
    next_url = None
    if more:
        last = cards[-1]
        next_url = url_for(
            "feed.index",
            view=view["id"] if view else "all",
            before_ts=last["last_updated"].isoformat(),
            before_id=last["id"],
        )
    context = {"views": all_views, "view": view, "cards": cards, "next_url": next_url}
    if before is not None:
        return render_template("partials/feed_page.html", **context)
    if is_htmx():
        return render_template("partials/feed_main.html", **context)
    return render_template("feed.html", **context)
