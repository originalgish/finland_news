from flask import Blueprint, g, redirect, request

from web.helpers import LANGS

bp = Blueprint("site", __name__)


@bp.get("/healthz")
def healthz():
    g.db.execute("SELECT 1")
    return "ok"


@bp.get("/lang/<code>")
def set_language(code: str):
    if code not in LANGS:
        return redirect("/")
    # Only same-site paths may be used as the destination.
    target = request.args.get("next", "/")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    response = redirect(target)
    response.set_cookie("lang", code, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response
