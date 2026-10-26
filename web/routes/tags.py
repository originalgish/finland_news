import psycopg
from flask import Blueprint, Response, abort, g, render_template, request

from web import queries
from web.helpers import checkbox, notice, parse_float, parse_int, require_htmx

bp = Blueprint("tags", __name__)

BACKFILL_DAYS = 30
PREVIEW_DAYS = 7


def _oob_notice(message: str) -> str:
    return f'<p id="notice" hx-swap-oob="true" class="notice ok">{message}</p>'


def _tag_row(tag_id: int) -> dict:
    row = next((t for t in queries.tags(g.db) if t["id"] == tag_id), None)
    if row is None:
        abort(404)
    return {"tag": row, "rules": queries.rules_by_tag(g.db).get(tag_id, [])}


def _article_tags_fragment(article_id: int) -> str:
    return render_template(
        "partials/article_tags.html",
        article_id=article_id,
        tags=queries.article_tags(g.db, article_id),
        tag_options=queries.tags(g.db),
    )


def _article_exists(article_id: int) -> bool:
    return g.db.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone() is not None


@bp.get("/tags")
def page():
    tag_rows = queries.tags(g.db)
    rules = queries.rules_by_tag(g.db)
    return render_template(
        "tags.html",
        tags=[{"tag": t, "rules": rules.get(t["id"], [])} for t in tag_rows],
    )


@bp.post("/tags")
@require_htmx
def create_tag():
    name = (request.form.get("name") or "").strip()
    if not name:
        return notice("A tag needs a name.", ok=False)
    try:
        with g.db.transaction():
            tag_id = g.db.execute(
                """
                INSERT INTO tags (name, weight, llm_assignable)
                VALUES (%s, %s, %s) RETURNING id
                """,
                (name, parse_float(request.form.get("weight"), 1.0), checkbox("llm_assignable")),
            ).fetchone()["id"]
    except psycopg.errors.UniqueViolation:
        return notice(f"The tag “{name}” already exists.", ok=False)
    return render_template("partials/tag_row.html", **_tag_row(tag_id))


@bp.patch("/tags/<int:tag_id>")
@require_htmx
def update_tag(tag_id: int):
    weight = parse_float(request.form.get("weight"), 1.0)
    g.db.execute(
        "UPDATE tags SET weight = %s, llm_assignable = %s WHERE id = %s",
        (weight, checkbox("llm_assignable"), tag_id),
    )
    return render_template("partials/tag_row.html", **_tag_row(tag_id))


@bp.delete("/tags/<int:tag_id>")
@require_htmx
def delete_tag(tag_id: int):
    g.db.execute("DELETE FROM tags WHERE id = %s", (tag_id,))
    return ""


@bp.post("/tags/<int:tag_id>/rules")
@require_htmx
def create_rule(tag_id: int):
    query = (request.form.get("query") or "").strip()
    if not query:
        return notice("A rule needs a search query.", ok=False)
    if g.db.execute("SELECT 1 FROM tags WHERE id = %s", (tag_id,)).fetchone() is None:
        abort(404)
    rule = g.db.execute(
        "INSERT INTO tag_rules (tag_id, query) VALUES (%s, %s) RETURNING id, tag_id, query, enabled",
        (tag_id, query),
    ).fetchone()
    params = {"tag": tag_id, "days": BACKFILL_DAYS, "query": query}
    matches = g.db.execute(
        """
        SELECT count(*) FROM articles a
        WHERE a.fetched_at > now() - make_interval(days => %(days)s)
          AND a.search_tsv @@ websearch_to_tsquery('finnish', %(query)s)
        """,
        params,
    ).fetchone()["count"]
    newly_tagged = g.db.execute(
        """
        INSERT INTO article_tags (article_id, tag_id, origin)
        SELECT a.id, %(tag)s, 'rule'::tag_origin
        FROM articles a
        WHERE a.fetched_at > now() - make_interval(days => %(days)s)
          AND a.search_tsv @@ websearch_to_tsquery('finnish', %(query)s)
        ON CONFLICT (article_id, tag_id) DO NOTHING
        """,
        params,
    ).rowcount
    body = render_template("partials/rule_row.html", rule=rule)
    message = (
        f"Rule added. {matches} articles from the last {BACKFILL_DAYS} days match, "
        f"{newly_tagged} newly tagged."
    )
    return Response(body + _oob_notice(message), mimetype="text/html")


@bp.get("/rules/preview")
def preview_rule():
    query = (request.args.get("query") or "").strip()
    if not query:
        return '<span class="muted">Type a query to see matches.</span>'
    count = queries.rule_preview_count(g.db, query)
    return f'<span class="muted">{count} articles match in the last {PREVIEW_DAYS} days.</span>'


@bp.delete("/rules/<int:rule_id>")
@require_htmx
def delete_rule(rule_id: int):
    g.db.execute("DELETE FROM tag_rules WHERE id = %s", (rule_id,))
    return ""


@bp.post("/articles/<int:article_id>/tags")
@require_htmx
def add_manual_tag(article_id: int):
    tag_id = parse_int(request.form.get("tag_id"))
    if not _article_exists(article_id) or tag_id is None:
        abort(404)
    # A manual tag takes over an existing feed, rule or LLM tag. Re-enrichment never removes it.
    g.db.execute(
        """
        INSERT INTO article_tags (article_id, tag_id, origin)
        VALUES (%s, %s, 'manual'::tag_origin)
        ON CONFLICT (article_id, tag_id) DO UPDATE SET origin = 'manual'::tag_origin
        """,
        (article_id, tag_id),
    )
    return _article_tags_fragment(article_id)


@bp.delete("/articles/<int:article_id>/tags/<int:tag_id>")
@require_htmx
def remove_tag(article_id: int, tag_id: int):
    if not _article_exists(article_id):
        abort(404)
    g.db.execute(
        "DELETE FROM article_tags WHERE article_id = %s AND tag_id = %s",
        (article_id, tag_id),
    )
    return _article_tags_fragment(article_id)
