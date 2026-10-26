"""One function per stage. Each takes a connection and the settings and returns an exit code.

The stages that need Claude return 1 when ANTHROPIC_API_KEY is missing, so a missing
key shows up as a failure rather than a silent skip.
"""

import psycopg

from app.config import Settings
from worker.cluster import run_cluster
from worker.digest import run_digest
from worker.embed import run_embed
from worker.enrich import make_same_event_judge, run_enrich
from worker.fetch import FetchOptions, FetchResult, run_fetch
from worker.health import ping
from worker.llm import Claude
from worker.maintenance import RETENTION_DAYS, run_maintenance
from worker.tagging import apply_tags


def fetch_sources(conn: psycopg.Connection, settings: Settings) -> list[FetchResult]:
    """Fetch every enabled source, print one line per source, and return the results."""
    results = run_fetch(conn, FetchOptions(user_agent=settings.user_agent))
    for result in results:
        line = (
            f"{result.source.name:<18} {result.outcome:<12} "
            f"items={result.items_found:<4} new={result.items_new:<4} "
            f"status={result.status_code} {result.error or ''}"
        )
        print(line.rstrip())
    succeeded = sum(result.success for result in results)
    print(f"{succeeded}/{len(results)} sources succeeded")
    return results


def fetch_stage(conn: psycopg.Connection, settings: Settings) -> int:
    fetch_sources(conn, settings)
    return 0


def embed_stage(conn: psycopg.Connection, settings: Settings) -> int:
    embedded = run_embed(conn, settings)
    print(f"embedded {embedded} articles")
    return 0


def cluster_stage(conn: psycopg.Connection, settings: Settings) -> int:
    llm = None
    same_event = None
    if settings.anthropic_api_key:
        llm = Claude.from_api_key(settings.anthropic_api_key, settings.llm_model)
        same_event = make_same_event_judge(conn, llm)
    else:
        print("same-event check off: ANTHROPIC_API_KEY is not set, so borderline pairs start new clusters")
    summary = run_cluster(conn, same_event=same_event)
    print(
        f"clustered {summary.articles} articles: {summary.joined} joined an existing cluster, "
        f"{summary.created} started a new one; {summary.asked} borderline, "
        f"{summary.asked_yes} confirmed"
    )
    if llm is not None:
        print(f"llm usage: {llm.usage.summary()}")
    return 0


def tag_stage(conn: psycopg.Connection, settings: Settings) -> int:
    summary = apply_tags(conn)
    print(
        f"tagged {summary.articles} new articles: {summary.feed} tags from feed categories, "
        f"{summary.rules} from keyword rules"
    )
    return 0


def enrich_stage(conn: psycopg.Connection, settings: Settings) -> int:
    if not settings.anthropic_api_key:
        print("enrich needs ANTHROPIC_API_KEY in .env")
        return 1
    llm = Claude.from_api_key(settings.anthropic_api_key, settings.llm_model)
    summary = run_enrich(conn, llm, settings.llm_max_articles_per_run)
    print(
        f"enriched {summary.articles} articles ({summary.retried} retried alone, "
        f"{summary.still_missing} still missing); wrote {summary.headlines} cluster headlines"
    )
    print(f"llm usage: {llm.usage.summary()}")
    return 0


def digest_stage(conn: psycopg.Connection, settings: Settings) -> int:
    if not settings.anthropic_api_key:
        print("digest needs ANTHROPIC_API_KEY in .env")
        return 1
    llm = Claude.from_api_key(settings.anthropic_api_key, settings.llm_model)
    summary = run_digest(conn, llm)
    print(
        f"digest for {summary.day}: {summary.stories} stories from {summary.candidates} "
        f"clusters updated in the last 24 hours; {summary.missing} left out for lack of an answer"
    )
    print(f"llm usage: {llm.usage.summary()}")
    # Pinged only after the digest is stored, so a failed run triggers the healthchecks alert.
    ping(settings.healthcheck_digest_url)
    return 0


def maintenance_stage(conn: psycopg.Connection, settings: Settings) -> int:
    pruned = run_maintenance(conn)
    print(f"pruned {pruned} fetch_log rows older than {RETENTION_DAYS} days; VACUUM ANALYZE done")
    return 0
