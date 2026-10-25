"""Command line entry point: python -m worker <stage>"""

import argparse
import sys

import psycopg

from app.config import get_settings
from worker.fetch import FetchOptions, run_fetch

STAGES = ("fetch",)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker")
    parser.add_argument("stage", choices=STAGES)
    parser.parse_args(argv)

    settings = get_settings()
    options = FetchOptions(user_agent=settings.user_agent)
    with psycopg.connect(settings.database_url) as conn:
        results = run_fetch(conn, options)

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


if __name__ == "__main__":
    sys.exit(main())
