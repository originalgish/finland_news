"""Command line entry point: python -m worker <stage>"""

import argparse
import sys
from collections.abc import Callable

import psycopg

from app.config import Settings, get_settings
from worker.cluster import run_cluster
from worker.embed import run_embed
from worker.fetch import FetchOptions, run_fetch


def fetch_stage(conn: psycopg.Connection, settings: Settings) -> int:
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
    return 0


def embed_stage(conn: psycopg.Connection, settings: Settings) -> int:
    embedded = run_embed(conn, settings)
    print(f"embedded {embedded} articles")
    return 0


def cluster_stage(conn: psycopg.Connection, settings: Settings) -> int:
    summary = run_cluster(conn)
    print(
        f"clustered {summary.articles} articles: {summary.joined} joined an existing cluster, "
        f"{summary.created} started a new one; {summary.asked} borderline, "
        f"{summary.asked_yes} confirmed"
    )
    return 0


STAGES: dict[str, Callable[[psycopg.Connection, Settings], int]] = {
    "fetch": fetch_stage,
    "embed": embed_stage,
    "cluster": cluster_stage,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker")
    parser.add_argument("stage", choices=sorted(STAGES))
    args = parser.parse_args(argv)

    settings = get_settings()
    with psycopg.connect(settings.database_url) as conn:
        return STAGES[args.stage](conn, settings)


if __name__ == "__main__":
    sys.exit(main())
