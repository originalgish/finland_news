import pytest

from worker.cluster import (
    BORDERLINE_FLOOR,
    MERGE_THRESHOLD,
    Candidate,
    Decision,
    decide,
)


def candidate(similarity, source_id=1):
    return Candidate(cluster_id=10, source_id=source_id, similarity=similarity)


def test_no_neighbour_starts_a_new_cluster():
    assert decide(None) is Decision.NEW


@pytest.mark.parametrize("similarity", [0.0, 0.3, BORDERLINE_FLOOR - 0.001])
def test_below_the_borderline_floor_starts_a_new_cluster(similarity):
    assert decide(candidate(similarity)) is Decision.NEW


@pytest.mark.parametrize("similarity", [BORDERLINE_FLOOR, 0.7, MERGE_THRESHOLD - 0.001])
def test_borderline_band_asks_the_same_event_check(similarity):
    assert decide(candidate(similarity)) is Decision.ASK


@pytest.mark.parametrize("similarity", [MERGE_THRESHOLD, 0.9, 0.99])
def test_at_or_above_the_merge_threshold_joins(similarity):
    assert decide(candidate(similarity)) is Decision.JOIN


def test_same_source_near_duplicate_joins_without_asking():
    assert decide(candidate(0.97, source_id=2)) is Decision.JOIN


def test_thresholds_are_ordered():
    assert 0 < BORDERLINE_FLOOR < MERGE_THRESHOLD < 1
