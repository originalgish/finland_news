"""Small request and display helpers shared by the routes and templates."""

from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from flask import Response, abort, g, request

LANGS = ("en", "fi")


def is_htmx() -> bool:
    return "HX-Request" in request.headers


def require_htmx[F: Callable[..., Any]](view: F) -> F:
    """Writes come from HTMX only. A plain cross-site form post cannot send this header."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not is_htmx():
            abort(403)
        return view(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def notice(message: str, *, ok: bool = True) -> Response:
    """A message for the page's notice area, sent with an HTMX retarget header."""
    css = "notice ok" if ok else "notice error"
    response = Response(f'<p class="{css}">{message}</p>', mimetype="text/html")
    response.headers["HX-Retarget"] = "#notice"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def display(english: str | None, finnish: str | None) -> str | None:
    """The English text when the reader chose English and it exists, else the Finnish text."""
    if g.lang == "en" and english:
        return english
    return finnish


def time_ago(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return ""
    seconds = ((now or datetime.now(UTC)) - value).total_seconds()
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def initials(name: str) -> str:
    words = name.replace("-", " ").split()
    return "".join(word[0] for word in words[:2]).upper()


def parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def checkbox(name: str) -> bool:
    return request.form.get(name) in ("on", "true", "1")
