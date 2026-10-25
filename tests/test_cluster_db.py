from datetime import UTC, datetime, timedelta
from math import sqrt

import numpy as np
import pytest

from app.migrate import migrate
from worker.cluster import cluster_pending
from worker.embed import to_pgvector

DIM = 384
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def vector_with_cosine(cosine, axis):
    """A unit vector with the given cosine to the shared story axis (index 0).

    Two vectors on different side axes have pairwise cosine cosine_a * cosine_b.
    """
    v = np.zeros(DIM)
    v[0] = cosine
    v[axis] = sqrt(1 - cosine * cosine)
    return to_pgvector(v)


def add(conn, source, index, embedding, published=NOW):
    source_id = conn.execute("SELECT id FROM sources WHERE name = %s", (source,)).fetchone()[0]
    url = f"https://example.fi/{source}/{index}"
    conn.execute(
        """
        INSERT INTO articles (source_id, url, url_canonical, title, published_at,
                              embedding, embedded_at)
        VALUES (%s, %s, %s, %s, %s, %s::vector, now())
        """,
        (source_id, url, url, f"Juttu {index}", published, embedding),
    )
    conn.commit()


def clusters(conn):
    return conn.execute(
        "SELECT id, article_count, source_count, headline FROM clusters ORDER BY id"
    ).fetchall()


def test_three_outlets_on_one_story_make_one_cluster(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.92, axis=1))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.92, axis=2))
    add(conn, "Iltalehti", 3, vector_with_cosine(0.92, axis=3))

    summary = cluster_pending(conn)

    assert (summary.articles, summary.created, summary.joined) == (3, 1, 2)
    ((_, article_count, source_count, headline),) = clusters(conn)
    assert (article_count, source_count, headline) == (3, 3, None)
    assert conn.execute("SELECT count(*) FROM articles WHERE clustered_at IS NULL").fetchone()[0] == 0


def test_the_centroid_is_the_running_mean_of_its_members(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.9, axis=1))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.9, axis=2))
    add(conn, "Iltalehti", 3, vector_with_cosine(0.9, axis=3))

    cluster_pending(conn)

    (centroid_text,) = conn.execute("SELECT centroid::text FROM clusters").fetchone()
    centroid = np.array(centroid_text.strip("[]").split(","), dtype=float)
    # Every member has 0.9 on the shared axis, so the mean keeps 0.9 there.
    assert centroid[0] == pytest.approx(0.9, abs=1e-4)


def test_unrelated_articles_start_separate_clusters(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.0, axis=1))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.0, axis=2))

    summary = cluster_pending(conn)

    assert (summary.created, summary.joined) == (2, 0)
    assert len(clusters(conn)) == 2


def test_borderline_pair_without_a_judge_starts_a_new_cluster(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.85, axis=1))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.85, axis=2))

    summary = cluster_pending(conn)

    assert (summary.asked, summary.asked_yes, summary.created) == (1, 0, 2)
    assert len(clusters(conn)) == 2


def test_borderline_pair_joins_when_the_judge_confirms(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.85, axis=1))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.85, axis=2))
    asked = []

    def judge(article_id, cluster_id):
        asked.append((article_id, cluster_id))
        return True

    summary = cluster_pending(conn, same_event=judge)

    assert (summary.asked, summary.asked_yes, summary.joined) == (1, 1, 1)
    assert len(asked) == 1
    assert len(clusters(conn)) == 1


def test_a_neighbour_older_than_72_hours_is_not_joined(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.95, axis=1), published=NOW - timedelta(hours=100))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.95, axis=2), published=NOW)

    summary = cluster_pending(conn)

    assert (summary.created, summary.joined) == (2, 0)


def test_a_neighbour_inside_72_hours_is_joined(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.95, axis=1), published=NOW - timedelta(hours=50))
    add(conn, "Ilta-Sanomat", 2, vector_with_cosine(0.95, axis=2), published=NOW)

    summary = cluster_pending(conn)

    assert (summary.created, summary.joined) == (1, 1)


def test_a_same_source_near_duplicate_joins_but_does_not_add_a_source(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.99, axis=1))
    add(conn, "Yle", 2, vector_with_cosine(0.99, axis=2))

    cluster_pending(conn)

    ((_, article_count, source_count, _),) = clusters(conn)
    assert (article_count, source_count) == (2, 1)


def test_a_second_run_has_nothing_to_do(conn):
    add(conn, "Yle", 1, vector_with_cosine(0.95, axis=1))
    cluster_pending(conn)

    summary = cluster_pending(conn)

    assert summary.articles == 0
