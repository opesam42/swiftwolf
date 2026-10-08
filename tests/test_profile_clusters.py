"""Adaptive Cluster Baselines: match / spawn / flag, no database."""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from src.profile.clusters import ClusterOutcome, evaluate_and_update_clusters, min_cluster_z
from src.profile.models import TypingCluster

K_MAX = 5
MATCH_Z = 2.5
ALPHA = 0.05
DEFAULT_STD = 15.0

positive = st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False, allow_infinity=False)


def _cluster(*, cluster_id: int = 1, avg: float = 90.0, std: float = 10.0, count: int = 10) -> TypingCluster:
    return TypingCluster(
        id=cluster_id,
        ewma_avg=avg,
        ewma_var=std * std,
        ewma_std=std,
        sample_count=count,
    )


def _run(clusters, x, k_max=K_MAX):
    return evaluate_and_update_clusters(
        clusters,
        x,
        k_max=k_max,
        match_z=MATCH_Z,
        alpha=ALPHA,
        default_std=DEFAULT_STD,
    )


def test_empty_list_spawns_first_cluster():
    clusters, outcome = _run([], 90.0)
    assert outcome is ClusterOutcome.SPAWN
    assert len(clusters) == 1
    assert clusters[0].ewma_avg == 90.0
    assert clusters[0].sample_count == 1
    assert clusters[0].ewma_std == 0.0


def test_near_value_matches_and_ewma_updates():
    original = _cluster(avg=90.0, std=10.0, count=20)
    clusters, outcome = _run([original], 95.0)
    assert outcome is ClusterOutcome.MATCH
    assert len(clusters) == 1
    assert clusters[0].sample_count == 21
    assert 90.0 < clusters[0].ewma_avg < 95.0


def test_far_value_spawns_until_k_max():
    clusters, outcome = _run([_cluster(avg=90.0, std=10.0)], 200.0)
    assert outcome is ClusterOutcome.SPAWN
    assert len(clusters) == 2
    assert clusters[1].ewma_avg == 200.0


def test_far_value_flags_when_slots_are_full():
    filled = [_cluster(cluster_id=i + 1, avg=80.0 + i * 5, std=5.0) for i in range(K_MAX)]
    snapshot = [c.model_dump() for c in filled]
    clusters, outcome = _run(filled, 500.0)
    assert outcome is ClusterOutcome.FLAG
    assert [c.model_dump() for c in clusters] == snapshot


def test_min_cluster_z_uses_nearest():
    clusters = [_cluster(avg=90.0, std=10.0), _cluster(cluster_id=2, avg=180.0, std=10.0)]
    assert min_cluster_z(clusters, 95.0, DEFAULT_STD) == pytest.approx(0.5)
    assert min_cluster_z([], 95.0, DEFAULT_STD) is None


def test_new_cluster_uses_default_std_for_z():
    """ewma_std is 0 on spawn; scoring/matching must not divide by zero."""
    spawned, _ = _run([], 90.0)
    z = min_cluster_z(spawned, 105.0, DEFAULT_STD)
    assert z == pytest.approx(15.0 / DEFAULT_STD)


@given(x=positive)
def test_flag_does_not_mutate_full_set(x):
    filled = [_cluster(cluster_id=i + 1, avg=50.0 + i, std=1.0) for i in range(K_MAX)]
    before = [c.model_dump() for c in filled]
    clusters, outcome = _run(filled, x + 10_000.0)
    assert outcome is ClusterOutcome.FLAG
    assert [c.model_dump() for c in clusters] == before


@given(xs=st.lists(positive, min_size=1, max_size=20))
@settings(max_examples=100)
def test_never_exceeds_k_max(xs):
    clusters: list[TypingCluster] = []
    for x in xs:
        clusters, _outcome = _run(clusters, x)
        assert len(clusters) <= K_MAX


@given(old=st.floats(min_value=20.0, max_value=200.0, allow_nan=False, allow_infinity=False))
def test_match_moves_mean_toward_sample(old):
    cluster = _cluster(avg=old, std=20.0, count=20)
    x = old + 10.0  # Z = 0.5, a match
    clusters, outcome = _run([cluster], x)
    assert outcome is ClusterOutcome.MATCH
    lo, hi = (old, x) if old <= x else (x, old)
    assert lo <= clusters[0].ewma_avg <= hi
