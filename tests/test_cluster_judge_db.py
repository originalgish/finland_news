from datetime import UTC, datetime
from math import sqrt

import numpy as np
import pytest
from fake_llm import FakeLLM, translate_all

from app.migrate import migrate
from worker.cluster import cluster_pending
from worker.embed import to_pgvector
from worker.enrich import make_same_event_judge

DIM = 384
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def vector_with_cosine(cosine, axis):
    v = np.zeros(DIM)
    v[0] = cosine
    v[axis] = sqrt(1 - cosine * cosine)
    return to_pgvector(v)


def add(conn, source, index, embedding):
    source_id = conn.execute("SELECT id FROM sources WHERE name = %s", (source,)).fetchone()[0]
    url = f"https://example.fi/{source}/{index}"
    conn.execute(
        """
        INSERT INTO articles (source_id, url, url_canonical, title, published_at,
                              embedding, embedded_at)
        VALUES (%s, %s, %s, %s, %s, %s::vector, now())
        """,
        (source_id, url, url, f"Juttu {index}", NOW, embedding),
    )
    conn.commit()


def test_a_confirmed_borderline_pair_joins_its_cluster(conn):
    # Cosine 0.85 falls between the floor (0.60) and the merge threshold (0.80).
    add(conn, "Yle", 1, vector_with_cosine(0.85, axis=1))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.85, axis=2))
    llm = FakeLLM(translate_all())

    summary = cluster_pending(conn, same_event=make_same_event_judge(conn, llm))

    assert (summary.asked, summary.asked_yes, summary.joined) == (1, 1, 1)
    assert conn.execute("SELECT count(*) FROM clusters").fetchone()[0] == 1
    assert [call["tool"] for call in llm.calls] == ["record_same_event"]


def test_a_rejected_borderline_pair_starts_its_own_cluster(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.85, axis=1))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.85, axis=2))
    llm = FakeLLM(lambda tool, user: {"same_event": False})

    summary = cluster_pending(conn, same_event=make_same_event_judge(conn, llm))

    assert (summary.asked, summary.asked_yes, summary.created) == (1, 0, 2)
    assert conn.execute("SELECT count(*) FROM clusters").fetchone()[0] == 2
