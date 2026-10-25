from datetime import UTC, datetime

import pytest

from app.config import DEFAULT_EMBED_MODEL
from app.migrate import migrate
from worker.dedupe import store_articles
from worker.embed import embed_pending, load_model
from worker.normalize import Article

DIM = 384
PUBLISHED = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


class FakeEmbedder:
    """Deterministic one-hot vectors, so tests need no model download."""

    def __init__(self, dimension=DIM):
        self.dimension = dimension
        self.batches = []

    def embed(self, documents):
        documents = list(documents)
        self.batches.append(documents)
        return [self._vector(text) for text in documents]

    def _vector(self, text):
        vector = [0.0] * self.dimension
        vector[sum(map(ord, text)) % self.dimension] = 1.0
        return vector


@pytest.fixture
def conn(scratch_conn):
    migrate(scratch_conn)
    return scratch_conn


def yle_id(conn):
    return conn.execute("SELECT id FROM sources WHERE name = 'Yle'").fetchone()[0]


def article(index, title, summary=None):
    url = f"https://yle.fi/a/{index}"
    return Article(
        url=url,
        url_canonical=url,
        guid=f"g{index}",
        title=title,
        summary=summary,
        feed_categories=[],
        published_at=PUBLISHED,
    )


def add(conn, *titles):
    store_articles(conn, yle_id(conn), [article(i, title) for i, title in enumerate(titles)])
    conn.commit()


def count_embedded(conn):
    return conn.execute("SELECT count(*) FROM articles WHERE embedded_at IS NOT NULL").fetchone()[0]


def test_every_pending_article_is_embedded_in_batches(conn):
    add(conn, "a", "b", "c", "d", "e")
    embedder = FakeEmbedder()

    embedded = embed_pending(conn, embedder, DIM, batch_size=2)

    assert embedded == 5
    assert [len(batch) for batch in embedder.batches] == [2, 2, 1]
    assert count_embedded(conn) == 5
    dims = conn.execute("SELECT DISTINCT vector_dims(embedding) FROM articles").fetchall()
    assert dims == [(DIM,)]


def test_a_second_run_has_nothing_to_do(conn):
    add(conn, "a", "b")
    embed_pending(conn, FakeEmbedder(), DIM)
    embedder = FakeEmbedder()

    assert embed_pending(conn, embedder, DIM) == 0
    assert embedder.batches == []


def test_rows_that_already_have_an_embedding_are_skipped(conn):
    add(conn, "a", "b", "c")
    conn.execute("UPDATE articles SET embedded_at = now() WHERE title = 'b'")
    embedder = FakeEmbedder()

    embed_pending(conn, embedder, DIM)

    sent = [text for batch in embedder.batches for text in batch]
    assert sorted(sent) == ["a", "c"]


def test_the_summary_is_part_of_the_embedded_text(conn):
    store_articles(conn, yle_id(conn), [article(1, "Otsikko", summary="Tiivistelmä")])
    conn.commit()
    embedder = FakeEmbedder()

    embed_pending(conn, embedder, DIM)

    assert embedder.batches == [["Otsikko\nTiivistelmä"]]


def test_wrong_dimension_is_rejected_and_nothing_is_written(conn):
    add(conn, "a", "b")

    with pytest.raises(ValueError, match="expected 384"):
        embed_pending(conn, FakeEmbedder(dimension=10), DIM)

    assert count_embedded(conn) == 0


def test_changed_text_schedules_a_new_embedding(conn):
    add(conn, "Vanha otsikko")
    embed_pending(conn, FakeEmbedder(), DIM)

    # Same text again: the embedding stays.
    store_articles(conn, yle_id(conn), [article(0, "Vanha otsikko")])
    assert count_embedded(conn) == 1

    # Changed headline: the embedding is marked stale.
    store_articles(conn, yle_id(conn), [article(0, "Uusi otsikko")])
    assert count_embedded(conn) == 0
    assert embed_pending(conn, FakeEmbedder(), DIM) == 1


def test_real_model_puts_a_paraphrase_next_to_the_original(conn):
    election = "Presidentinvaalien ehdokkaat kävivät väittelyn televisiossa"
    paraphrase = "Presidenttiehdokkaat väittelivät tv-lähetyksessä vaalien alla"
    add(conn, election, "HIFK voitti jääkiekko-ottelun Helsingissä", "Sää lämpenee ensi viikolla Etelä-Suomessa", paraphrase)

    embed_pending(conn, load_model(DEFAULT_EMBED_MODEL, None), DIM)

    nearest = conn.execute(
        """
        SELECT title FROM articles
        ORDER BY embedding <=> (SELECT embedding FROM articles WHERE title = %s)
        LIMIT 2
        """,
        (election,),
    ).fetchall()
    # The first row is the article itself; the second should be its paraphrase.
    assert nearest[0][0] == election
    assert nearest[1][0] == paraphrase
