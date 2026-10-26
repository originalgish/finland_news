"""Command line entry point: python -m worker <stage>"""

import argparse
import sys
from collections.abc import Callable

import psycopg

from app.config import Settings, get_settings
from worker.pipeline import run_pipeline
from worker.stages import (
    cluster_stage,
    digest_stage,
    embed_stage,
    enrich_stage,
    fetch_stage,
    maintenance_stage,
    tag_stage,
)

STAGES: dict[str, Callable[[psycopg.Connection, Settings], int]] = {
    "fetch": fetch_stage,
    "embed": embed_stage,
    "cluster": cluster_stage,
    "tag": tag_stage,
    "enrich": enrich_stage,
    "digest": digest_stage,
    "run": run_pipeline,
    "maintenance": maintenance_stage,
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
