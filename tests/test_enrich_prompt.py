from worker.enrich import TAG_DEFINITIONS, enrichment_system

STARTER_TAGS = {
    "politics", "economy", "business", "sports", "culture", "crime",
    "health", "tech", "world", "local", "weather",
}


def test_every_starter_tag_has_a_definition():
    assert set(TAG_DEFINITIONS) == STARTER_TAGS


def test_the_prompt_lists_each_tag_with_its_definition():
    prompt = enrichment_system(sorted(STARTER_TAGS))

    for name in STARTER_TAGS:
        assert f"  {name}: {TAG_DEFINITIONS[name]}" in prompt


def test_a_tag_without_a_definition_is_listed_by_name():
    prompt = enrichment_system(["custom"])

    assert "  custom: custom" in prompt


def test_the_prompt_requires_at_least_one_tag_and_a_summary_translation():
    prompt = enrichment_system(sorted(STARTER_TAGS))

    assert "at least one and at most three tags" in prompt
    assert "Translate every summary" in prompt


def test_crime_excludes_accidents_fires_and_disasters():
    definition = TAG_DEFINITIONS["crime"]

    assert "Accidents, fires, disasters and sporting incidents are never crime" in definition
