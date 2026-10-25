"""Stage 3: embed each article's text so clustering and search can compare articles.

Each article is embedded as its Finnish title, then its summary. Rows whose
embedded_at is NULL are processed in batches, so an interrupted run resumes
where it stopped. Model weights load once per process.
"""

import functools
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

import psycopg

from app.config import Settings

PENDING_SQL = """
SELECT id, title, summary
FROM articles
WHERE embedded_at IS NULL
ORDER BY id
LIMIT %s
"""

WRITE_SQL = """
UPDATE articles
SET embedding = %s::vector, embedded_at = now()
WHERE id = %s
"""


class Embedder(Protocol):
    def embed(self, documents: Sequence[str]) -> Iterable[Any]: ...


@functools.cache
def load_model(model_name: str, cache_dir: str | None = None) -> Embedder:
    # Imported here so stages that do not embed do not load the ONNX runtime.
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model_name, cache_dir=cache_dir)


def embedding_text(title: str, summary: str | None) -> str:
    return "\n".join(part for part in (title, summary) if part)


def to_pgvector(vector: Iterable[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def embed_pending(
    conn: psycopg.Connection,
    embedder: Embedder,
    dimension: int,
    batch_size: int = 256,
) -> int:
    """Embed every article that has no embedding yet. Returns how many were embedded."""
    embedded = 0
    while True:
        rows = conn.execute(PENDING_SQL, (batch_size,)).fetchall()
        if not rows:
            return embedded

        texts = [embedding_text(title, summary) for _, title, summary in rows]
        vectors = list(embedder.embed(texts))
        for vector in vectors:
            if len(vector) != dimension:
                raise ValueError(f"model returned {len(vector)} dimensions, expected {dimension}")

        with conn.cursor() as cur:
            cur.executemany(
                WRITE_SQL,
                [(to_pgvector(v), row[0]) for row, v in zip(rows, vectors, strict=True)],
            )
        conn.commit()
        embedded += len(rows)


def run_embed(conn: psycopg.Connection, settings: Settings) -> int:
    embedder = load_model(settings.embed_model, settings.embed_cache_dir)
    return embed_pending(conn, embedder, settings.embed_dim)
