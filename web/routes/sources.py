from flask import Blueprint, abort, g, render_template

from web import queries
from web.helpers import require_htmx

bp = Blueprint("sources", __name__)


def _row(source_id: int) -> str:
    source = next((s for s in queries.sources(g.db) if s["id"] == source_id), None)
    if source is None:
        abort(404)
    return render_template(
        "partials/source_row.html",
        source=source,
        log=queries.fetch_log(g.db, source_id),
    )


@bp.get("/sources")
def page():
    rows = [
        {"source": s, "log": queries.fetch_log(g.db, s["id"])}
        for s in queries.sources(g.db)
    ]
    return render_template("sources.html", rows=rows)


@bp.post("/sources/<int:source_id>/toggle")
@require_htmx
def toggle(source_id: int):
    # Turning a source back on clears its failure count, so it gets a fresh start.
    updated = g.db.execute(
        """
        UPDATE sources
        SET enabled = NOT enabled,
            consecutive_failures = CASE WHEN enabled THEN consecutive_failures ELSE 0 END,
            disabled_reason = CASE WHEN enabled THEN disabled_reason ELSE NULL END
        WHERE id = %s
        """,
        (source_id,),
    ).rowcount
    if not updated:
        abort(404)
    return _row(source_id)
