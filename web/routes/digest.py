"""The daily digest: a page for each day, and an RSS feed of the recent days.

The worker stores each day's page as HTML, so these routes only read it back.
"""

from datetime import date
from email.utils import format_datetime
from typing import Any
from xml.etree import ElementTree as ET

from flask import Blueprint, Response, abort, g, render_template, url_for

from web import queries

bp = Blueprint("digest", __name__)
FEED_DAYS = 14


@bp.get("/digest")
def latest():
    return _page(queries.digest(g.db))


@bp.get("/digest/<day>")
def on_day(day: str):
    try:
        wanted = date.fromisoformat(day)
    except ValueError:
        abort(404)
    stored = queries.digest(g.db, wanted)
    if stored is None:
        abort(404)
    return _page(stored)


@bp.get("/digest.xml")
def feed():
    return Response(_rss(queries.recent_digests(g.db, FEED_DAYS)), mimetype="application/rss+xml")


def _page(stored: dict[str, Any] | None):
    return render_template("digest.html", digest=stored, days=queries.digest_dates(g.db))


def _rss(digests: list[dict[str, Any]]) -> str:
    """One RSS item per day, newest first. The description is the stored page."""
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Finland News digest"
    ET.SubElement(channel, "link").text = url_for("digest.latest", _external=True)
    ET.SubElement(channel, "description").text = "The stories that mattered most each day, from Finnish outlets."
    for stored in digests:
        day = stored["date"].isoformat()
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"Finland News digest, {day}"
        ET.SubElement(item, "link").text = url_for("digest.on_day", day=day, _external=True)
        ET.SubElement(item, "guid", isPermaLink="false").text = f"digest-{day}"
        ET.SubElement(item, "pubDate").text = format_datetime(stored["created_at"])
        ET.SubElement(item, "description").text = stored["html"]
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(rss, encoding="unicode")
