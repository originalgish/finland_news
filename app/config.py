"""Settings from the process environment, with a local .env file as fallback."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str


def get_settings() -> Settings:
    # load_dotenv never overrides variables already set, so compose's env_file wins.
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return Settings(database_url=database_url)
