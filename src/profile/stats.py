"""Online amount-baseline updates. Pure functions — no database.

Welford is the lifetime mean. EWMA (Finch/West) is the recent-habit mean
the Z-score reads. Both take amounts in naira.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WelfordState:
    count: int
    avg: float
    m2: float
    std: float


@dataclass(frozen=True, slots=True)
class EwmaState:
    avg: float
    var: float
    std: float


def welford_update(count: int, avg: float, m2: float, x: float) -> WelfordState:
    """One equal-weight sample of the lifetime mean and sample std."""
    new_count = count + 1
    delta = x - avg
    new_avg = avg + (delta / new_count)
    new_m2 = m2 + (delta * (x - new_avg))
    variance = (new_m2 / (new_count - 1)) if new_count > 1 else 0.0
    return WelfordState(count=new_count, avg=new_avg, m2=new_m2, std=math.sqrt(variance))


def ewma_update(n: int, avg: float, var: float, x: float, alpha: float) -> EwmaState:
    """One exponentially weighted sample.

    `n` is the lifetime count *after* this observation (same `count` Welford
    just produced). Until n reaches 1/alpha, α_t = 1/n so the first ~20
    samples match Welford's mean; after that α_t is the configured alpha.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    if not 0.0 < alpha <= 1.0:
        raise ValueError("alpha must be in (0, 1]")

    if n == 1:
        return EwmaState(avg=x, var=0.0, std=0.0)

    alpha_t = max(alpha, 1.0 / n)
    delta = x - avg
    new_avg = avg + alpha_t * delta
    new_var = (1.0 - alpha_t) * (var + alpha_t * delta * delta)
    return EwmaState(avg=new_avg, var=new_var, std=math.sqrt(new_var))
