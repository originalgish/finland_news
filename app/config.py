"""Settings from the process environment, with a local .env file as fallback."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_EMBED_DIM = 384


@dataclass(frozen=True)
class Settings:
    database_url: str
    user_agent: str
    embed_model: str
    embed_dim: int
    embed_cache_dir: str | None


def get_settings() -> Settings:
    # load_dotenv never overrides variables already set, so compose's env_file wins.
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return Settings(
        database_url=database_url,
        user_agent=os.environ.get("USER_AGENT", "FinlandToday/0.1"),
        embed_model=os.environ.get("EMBED_MODEL", DEFAULT_EMBED_MODEL),
        embed_dim=int(os.environ.get("EMBED_DIM", DEFAULT_EMBED_DIM)),
        embed_cache_dir=os.environ.get("EMBED_CACHE_DIR") or None,
    )
