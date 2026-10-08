"""Online Adaptive K-Means with EWMA decay (Adaptive Cluster Baselines).

One-dimensional: each telemetry field has its own list of clusters, capped at
K_max. Distance is a Z-score against cluster μ/σ. Pure functions — no database.
"""

from __future__ import annotations

from enum import Enum

from src.profile.models import TypingCluster
from src.profile.stats import ewma_update

TYPING_FIELDS = (
    "dwell_time_ms",
    "flight_time_ms",
    "time_to_first_keystroke_ms",
    "backspace_count",
)


class ClusterOutcome(str, Enum):
    MATCH = "match"
    SPAWN = "spawn"
    FLAG = "flag"


def _sigma(cluster: TypingCluster, default_std: float) -> float:
    return cluster.ewma_std if cluster.ewma_std > 0 else default_std


def min_cluster_z(
    clusters: list[TypingCluster],
    x: float,
    default_std: float,
) -> float | None:
    """Nearest-cluster Z-score, or None when the field has no clusters yet."""
    if not clusters:
        return None
    return min(abs(x - cluster.ewma_avg) / _sigma(cluster, default_std) for cluster in clusters)


def evaluate_and_update_clusters(
    clusters: list[TypingCluster],
    x: float,
    *,
    k_max: int,
    match_z: float,
    alpha: float,
    default_std: float,
) -> tuple[list[TypingCluster], ClusterOutcome]:
    """Match (EWMA-update nearest), spawn a new cluster, or flag (no mutation)."""
    existing = list(clusters)

    if not existing:
        if k_max < 1:
            return existing, ClusterOutcome.FLAG
        return [_new_cluster(cluster_id=1, x=x)], ClusterOutcome.SPAWN

    nearest_index, nearest_z = min(
        (
            (index, abs(x - cluster.ewma_avg) / _sigma(cluster, default_std))
            for index, cluster in enumerate(existing)
        ),
        key=lambda item: item[1],
    )

    if nearest_z <= match_z:
        matched = existing[nearest_index]
        n = matched.sample_count + 1
        recent = ewma_update(n, matched.ewma_avg, matched.ewma_var, x, alpha)
        existing[nearest_index] = matched.model_copy(
            update={
                "ewma_avg": round(recent.avg, 2),
                "ewma_var": recent.var,
                "ewma_std": round(recent.std, 2),
                "sample_count": n,
            }
        )
        return existing, ClusterOutcome.MATCH

    if len(existing) < k_max:
        next_id = max(cluster.id for cluster in existing) + 1
        existing.append(_new_cluster(cluster_id=next_id, x=x))
        return existing, ClusterOutcome.SPAWN

    return existing, ClusterOutcome.FLAG


def _new_cluster(*, cluster_id: int, x: float) -> TypingCluster:
    return TypingCluster(
        id=cluster_id,
        ewma_avg=round(x, 2),
        ewma_var=0.0,
        ewma_std=0.0,
        sample_count=1,
    )
