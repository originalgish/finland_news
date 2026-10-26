"""Flask app factory.

The web app only reads the database, and the worker writes to it. Postgres is the only
link between them. Reads go through queries.py. Writes are small HTMX requests.
"""

from contextlib import ExitStack

from flask import Flask, g, request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings
from web.helpers import LANGS, display, initials, time_ago
from web.routes import clusters, feed, search, site, sources, tags, views


def create_app(database_url: str | None = None) -> Flask:
    app = Flask(__name__)
    url = database_url or get_settings().database_url
    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": dict_row},
        open=True,
    )
    app.extensions["db_pool"] = pool

    app.jinja_env.filters["time_ago"] = time_ago
    app.jinja_env.filters["initials"] = initials
    app.jinja_env.globals["display"] = display

    @app.before_request
    def open_request_state() -> None:
        # One transaction per request. It commits on success and rolls back on error.
        g.lang = request.cookies.get("lang") if request.cookies.get("lang") in LANGS else "en"
        g.transaction = ExitStack()
        g.db = g.transaction.enter_context(pool.connection())

    @app.teardown_request
    def close_request_state(error: BaseException | None) -> None:
        transaction = g.pop("transaction", None)
        if transaction is None:
            return
        if error is not None:
            g.db.rollback()
        transaction.close()

    for blueprint in (feed.bp, clusters.bp, tags.bp, views.bp, sources.bp, search.bp, site.bp):
        app.register_blueprint(blueprint)

    return app
