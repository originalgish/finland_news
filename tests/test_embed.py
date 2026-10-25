from worker.embed import embedding_text, to_pgvector


def test_embedding_text_is_title_then_summary():
    assert embedding_text("Otsikko", "Tiivistelmä") == "Otsikko\nTiivistelmä"


def test_embedding_text_without_summary_is_just_the_title():
    assert embedding_text("Otsikko", None) == "Otsikko"


def test_to_pgvector_writes_a_vector_literal():
    assert to_pgvector([0.5, 1.0, -0.25]) == "[0.5,1.0,-0.25]"
