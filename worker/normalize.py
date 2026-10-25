"""Stage 2, part 1: turn a parsed feed entry into a clean article record.

Pure functions only. Storage and deduplication live in dedupe.py.
"""

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import struct_time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser

FEED_TIMEZONE = ZoneInfo("Europe/Helsinki")
SUMMARY_LIMIT = 500

# Tracking parameters removed from article URLs. Yle appends origin=rss to RSS links.
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "origin"}
TRACKING_PREFIXES = ("utm_",)

SOFT_HYPHEN = "­"
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Article:
    url: str
    url_canonical: str
    guid: str | None
    title: str
    summary: str | None
    feed_categories: list[str]
    published_at: datetime


def canonical_url(url: str) -> str:
    """The form used to recognise the same article across feeds and re-runs.

    Lower-cases scheme and host, drops fragments, tracking parameters and
    AMP markers, sorts the remaining query, and removes a trailing slash.
    """
    parts = urlsplit(url.strip())
    path = parts.path
    if "/amp/" in path:
        path = path.replace("/amp/", "/", 1)
    path = path.removesuffix("/amp").rstrip("/")

    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(key) and key != "amp"
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES)


def clean_text(value: str | None) -> str:
    """Decode HTML entities, drop soft hyphens, and collapse all whitespace.

    Soft hyphens are invisible, but they split words for search and stemming.
    """
    if not value:
        return ""
    unescaped = html.unescape(value).replace(SOFT_HYPHEN, "")
    return _WHITESPACE.sub(" ", unescaped).strip()


def clean_summary(value: str | None) -> str | None:
    """Plain text from a teaser: tags removed, entities decoded, cut at a word boundary."""
    if not value:
        return None
    text = clean_text(LexborHTMLParser(value).text(separator=" "))
    if not text:
        return None
    return _truncate(text, SUMMARY_LIMIT)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit + 1)
    return text[:cut] if cut > 0 else text[:limit]


def parse_published(
    raw: str | None,
    parsed: struct_time | None,
    fetched_at: datetime,
    now: datetime,
) -> datetime:
    """Publication time in UTC.

    Missing dates fall back to fetched_at. Naive dates are read as Helsinki
    time. Future dates are clamped to now.
    """
    value = _parse_date_string(raw)
    if value is None and parsed is not None:
        # feedparser already converted this to UTC, e.g. for "25 Oct 26 14:00 UTC".
        value = datetime(*parsed[:6], tzinfo=UTC)
    if value is None:
        return fetched_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=FEED_TIMEZONE)
    return min(value.astimezone(UTC), now)


def _parse_date_string(raw: str | None) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def feed_categories(entry: dict[str, Any]) -> list[str]:
    """Raw category strings as the feed sent them, in order, without repeats."""
    terms: list[str] = []
    for tag in entry.get("tags") or []:
        term = clean_text(tag.get("term"))
        if term and term not in terms:
            terms.append(term)
    return terms


def normalize_entries(
    entries: Iterable[dict[str, Any]],
    fetched_at: datetime,
    now: datetime,
) -> list[Article]:
    """Normalize one feed's entries. Entries without a usable link or title are skipped."""
    entries = list(entries)
    links = [(entry.get("link") or "").strip() for entry in entries]
    articles = []
    for entry, link in zip(entries, links, strict=True):
        title = clean_text(entry.get("title"))
        if not link or not title:
            continue
        try:
            canonical = canonical_url(link)
        except ValueError:
            continue
        articles.append(
            Article(
                url=link,
                url_canonical=canonical,
                guid=_guid(entry.get("id"), link, links),
                title=title,
                summary=clean_summary(entry.get("summary")),
                feed_categories=feed_categories(entry),
                published_at=parse_published(
                    entry.get("published") or entry.get("updated"),
                    entry.get("published_parsed") or entry.get("updated_parsed"),
                    fetched_at,
                    now,
                ),
            )
        )
    return articles


def _guid(raw_id: str | None, own_link: str, all_links: list[str]) -> str | None:
    """The feed's own id, unless it is the link of a different item in the same feed.

    Some feeds reuse another item's URL as the id. Such an id cannot tell items
    apart, so it is dropped.
    """
    guid = (raw_id or "").strip()
    if not guid:
        return None
    if guid != own_link and guid in all_links:
        return None
    return guid
