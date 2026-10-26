"""Stage 7: the daily digest, the stories most worth reading from the last 24 hours.

Each story with an article in the window is scored from how many outlets cover it,
how recently it was updated, and the weight of its most important tag. The top
stories go to Claude in one call, which writes an English headline and a short blurb
for each. The page is rendered once and stored under today's date in Helsinki time.
Its items are kept as JSON for the RSS feed. A second run on the same day replaces
the first, and a run that gets no usable answer keeps the previous digest.
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import psycopg
from jinja2 import Environment, FileSystemLoader
from psycopg.types.json import Jsonb

from worker.llm import Claude, LLMError

DIGEST_TIMEZONE = ZoneInfo("Europe/Helsinki")
WINDOW = timedelta(hours=24)
DIGEST_SIZE = 8
# Score weights: the plan's starting values, to tune after a week of digests. A story's score is
#   SOURCE_WEIGHT * source_count + RECENCY_WEIGHT * recency + TAG_WEIGHT * tag_weight
SOURCE_WEIGHT = 2.0
RECENCY_WEIGHT = 1.0
TAG_WEIGHT = 1.0
RECENCY_HALF_LIFE_HOURS = 8.0
ARTICLES_PER_STORY = 6  # titles and summaries sent to Claude per story
DIGEST_MAX_TOKENS = 2048

CANDIDATES_SQL = """
SELECT c.id, c.last_updated, c.source_count, COALESCE(max(t.weight), 1.0)::float8
FROM clusters c
LEFT JOIN articles a ON a.cluster_id = c.id
LEFT JOIN article_tags at ON at.article_id = a.id
LEFT JOIN tags t ON t.id = at.tag_id
WHERE c.article_count > 0
  AND c.last_updated > %s
GROUP BY c.id
"""

MEMBERS_SQL = """
SELECT a.cluster_id, a.title, a.title_en, a.summary, a.summary_en, a.url, s.name
FROM articles a JOIN sources s ON s.id = a.source_id
WHERE a.cluster_id = ANY(%s)
ORDER BY a.cluster_id, a.published_at DESC
"""

WRITE_SQL = """
INSERT INTO digests (date, html, items)
VALUES (%(date)s, %(html)s, %(items)s)
ON CONFLICT (date) DO UPDATE
SET html = EXCLUDED.html, items = EXCLUDED.items, created_at = now()
"""

DIGEST_SYSTEM = (
    "You write the daily news digest for an English-speaking reader of Finnish news. You receive "
    "stories. Each story has the headlines and summaries of its articles, in English where "
    "available and in Finnish otherwise. For every story, write a neutral English headline of at "
    "most twelve words and a blurb of one or two sentences saying what happened. Use only what "
    "the text says. Call record_digest once, with one entry per story id."
)

DIGEST_TOOL: dict[str, Any] = {
    "name": "record_digest",
    "description": "Record a headline and a blurb for each story.",
    "input_schema": {
        "type": "object",
        "properties": {
            "stories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "headline": {"type": "string"},
                        "blurb": {"type": "string"},
                    },
                    "required": ["id", "headline", "blurb"],
                },
            }
        },
        "required": ["stories"],
    },
}

_TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True,
)


class Candidate(NamedTuple):
    cluster_id: int
    last_updated: datetime
    source_count: int
    tag_weight: float


@dataclass
class DigestSummary:
    day: date | None = None
    candidates: int = 0
    stories: int = 0
    missing: int = 0


def score_story(candidate: Candidate, now: datetime) -> float:
    hours_since_update = max(0.0, (now - candidate.last_updated).total_seconds() / 3600)
    # Exponential decay that halves every RECENCY_HALF_LIFE_HOURS.
    recency = 0.5 ** (hours_since_update / RECENCY_HALF_LIFE_HOURS)
    return (
        SOURCE_WEIGHT * candidate.source_count
        + RECENCY_WEIGHT * recency
        + TAG_WEIGHT * candidate.tag_weight
    )


def pick_stories(candidates: list[Candidate], now: datetime, size: int = DIGEST_SIZE) -> list[int]:
    """The cluster ids of the best stories, best first. Ties go to the newer cluster."""
    ranked = sorted(candidates, key=lambda c: (-score_story(c, now), -c.cluster_id))
    return [candidate.cluster_id for candidate in ranked[:size]]


def digest_day(now: datetime) -> date:
    """The Helsinki calendar day, so a run after midnight local time starts the next digest."""
    return now.astimezone(DIGEST_TIMEZONE).date()


def parse_digest(reply: dict[str, Any], sent_ids: set[int]) -> dict[int, tuple[str, str]]:
    """Headline and blurb per story id. Unknown ids and empty answers are ignored."""
    found: dict[int, tuple[str, str]] = {}
    for item in reply.get("stories") or []:
        if not isinstance(item, dict):
            continue
        story_id = item.get("id")
        if not isinstance(story_id, int) or story_id not in sent_ids or story_id in found:
            continue
        headline = str(item.get("headline") or "").strip()
        blurb = str(item.get("blurb") or "").strip()
        if headline and blurb:
            found[story_id] = (headline, blurb)
    return found


def _members_by_story(conn: psycopg.Connection, story_ids: list[int]) -> dict[int, list[dict[str, str]]]:
    members: dict[int, list[dict[str, str]]] = {}
    rows = conn.execute(MEMBERS_SQL, (story_ids,)).fetchall()
    for cluster_id, title, title_en, summary, summary_en, url, source in rows:
        members.setdefault(cluster_id, []).append(
            {
                "title": title_en or title,
                "summary": summary_en or summary or "",
                "url": url,
                "source": source,
            }
        )
    return members


def build_items(
    story_ids: list[int],
    written: dict[int, tuple[str, str]],
    members: dict[int, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    """The stories that got an answer, in ranking order, with links to their articles."""
    items = []
    for story_id in story_ids:
        if story_id not in written:
            continue
        headline, blurb = written[story_id]
        links = [
            {"source": member["source"], "title": member["title"], "url": member["url"]}
            for member in members.get(story_id, [])
            if member["url"].startswith(("https://", "http://"))
        ]
        items.append({"cluster_id": story_id, "headline": headline, "blurb": blurb, "links": links})
    return items


def render_digest(items: list[dict[str, Any]]) -> str:
    return _TEMPLATES.get_template("digest_body.html").render(items=items)


def run_digest(conn: psycopg.Connection, llm: Claude, now: datetime | None = None) -> DigestSummary:
    """Choose today's stories, have Claude write them up, and store the digest for the day."""
    now = now or datetime.now(UTC)
    day = digest_day(now)
    candidates = [
        Candidate(*row) for row in conn.execute(CANDIDATES_SQL, (now - WINDOW,)).fetchall()
    ]
    chosen = pick_stories(candidates, now)
    summary = DigestSummary(day=day, candidates=len(candidates))

    items: list[dict[str, Any]] = []
    if chosen:
        members = _members_by_story(conn, chosen)
        payload = [
            {
                "id": story_id,
                "articles": [
                    {"title": member["title"], "summary": member["summary"]}
                    for member in members.get(story_id, [])[:ARTICLES_PER_STORY]
                ],
            }
            for story_id in chosen
        ]
        reply = llm.call_tool(
            system=DIGEST_SYSTEM,
            user=json.dumps(payload, ensure_ascii=False),
            tool=DIGEST_TOOL,
            max_tokens=DIGEST_MAX_TOKENS,
        )
        items = build_items(chosen, parse_digest(reply, set(chosen)), members)
        if not items:
            # Storing an empty digest would replace yesterday's good one with nothing.
            raise LLMError("the model wrote none of the digest stories; keeping the previous digest")

    summary.stories = len(items)
    summary.missing = len(chosen) - len(items)
    conn.execute(WRITE_SQL, {"date": day, "html": render_digest(items), "items": Jsonb(items)})
    conn.commit()
    return summary
