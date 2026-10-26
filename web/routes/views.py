import psycopg
from flask import Blueprint, Response, abort, g, render_template, request

from web import queries
from web.helpers import checkbox, notice, parse_int, require_htmx

bp = Blueprint("views", __name__)


def _id_list(field: str) -> list[int]:
    return [i for i in (parse_int(v) for v in request.form.getlist(field)) if i is not None]


def _view_fields() -> dict:
    return {
        "name": (request.form.get("name") or "").strip(),
        "include_tags": _id_list("include_tags"),
        "include_sources": _id_list("include_sources"),
        "is_default": checkbox("is_default"),
        "position": parse_int(request.form.get("position")) or 0,
    }


def _clear_other_defaults(keep_id: int | None) -> None:
    g.db.execute(
        "UPDATE views SET is_default = false WHERE is_default AND (%s::int IS NULL OR id <> %s)",
        (keep_id, keep_id),
    )


def _row(view_id: int) -> str:
    view = next((v for v in queries.views(g.db) if v["id"] == view_id), None)
    if view is None:
        abort(404)
    return render_template("partials/view_row.html", view=view, **_options())


def _options() -> dict:
    return {"tag_options": queries.tags(g.db), "source_options": _sources()}


def _sources() -> list[dict]:
    return g.db.execute("SELECT id, name FROM sources ORDER BY id").fetchall()


@bp.get("/views")
def page():
    return render_template("views.html", views=queries.views(g.db), **_options())


@bp.post("/views")
@require_htmx
def create_view():
    fields = _view_fields()
    if not fields["name"]:
        return notice("A view needs a name.", ok=False)
    try:
        with g.db.transaction():
            if fields["is_default"]:
                _clear_other_defaults(None)
            view_id = g.db.execute(
                """
                INSERT INTO views (name, include_tags, include_sources, is_default, position)
                VALUES (%(name)s, %(include_tags)s::int[], %(include_sources)s::int[],
                        %(is_default)s, %(position)s)
                RETURNING id
                """,
                fields,
            ).fetchone()["id"]
    except psycopg.errors.UniqueViolation:
        return notice(f"A view named “{fields['name']}” already exists.", ok=False)
    return Response(_row(view_id), mimetype="text/html")


@bp.patch("/views/<int:view_id>")
@require_htmx
def update_view(view_id: int):
    fields = _view_fields()
    if not fields["name"]:
        return notice("A view needs a name.", ok=False)
    try:
        with g.db.transaction():
            if fields["is_default"]:
                _clear_other_defaults(view_id)
            updated = g.db.execute(
                """
                UPDATE views
                SET name = %(name)s, include_tags = %(include_tags)s::int[],
                    include_sources = %(include_sources)s::int[],
                    is_default = %(is_default)s, position = %(position)s
                WHERE id = %(id)s
                """,
                {**fields, "id": view_id},
            ).rowcount
    except psycopg.errors.UniqueViolation:
        return notice(f"A view named “{fields['name']}” already exists.", ok=False)
    if not updated:
        abort(404)
    return Response(_row(view_id), mimetype="text/html")


@bp.delete("/views/<int:view_id>")
@require_htmx
def delete_view(view_id: int):
    g.db.execute("DELETE FROM views WHERE id = %s", (view_id,))
    return ""
