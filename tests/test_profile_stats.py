"""Pure Welford / EWMA updates — no database.

Named examples pin the α = 0.05 product story. Hypothesis checks the
invariants on random amount streams.
"""

from __future__ import annotations

import math
import statistics

import pytest
from hypothesis import assume, given, settings, strategies as st

from src.profile.stats import ewma_update, welford_update

ALPHA = 0.05
WARMUP_MAX = 19  # last n where α_t = 1/n because 1/0.05 = 20

# Production amounts are integer kobo stored as naira (kobo / 100).
naira = st.integers(min_value=1, max_value=10_000_000).map(lambda kobo: kobo / 100.0)
amount_streams = st.lists(naira, min_size=1, max_size=40)


def _fold_welford(xs: list[float]):
    state = welford_update(0, 0.0, 0.0, xs[0])
    for x in xs[1:]:
        state = welford_update(state.count, state.avg, state.m2, x)
    return state


def _fold_ewma(xs: list[float], alpha: float = ALPHA):
    state = ewma_update(1, 0.0, 0.0, xs[0], alpha)
    for n, x in enumerate(xs[1:], start=2):
        state = ewma_update(n, state.avg, state.var, x, alpha)
    return state


# --- Named examples (the α = 0.05 spec) ---


def test_welford_first_sample_has_zero_std():
    state = welford_update(count=0, avg=0.0, m2=0.0, x=500.0)
    assert state.count == 1
    assert state.avg == 500.0
    assert state.m2 == 0.0
    assert state.std == 0.0


def test_welford_two_samples_match_sample_std():
    state = welford_update(0, 0.0, 0.0, 100.0)
    state = welford_update(state.count, state.avg, state.m2, 300.0)
    assert state.count == 2
    assert state.avg == 200.0
    assert state.std == math.sqrt(20000.0)


def test_ewma_first_sample_has_zero_std():
    state = ewma_update(n=1, avg=0.0, var=0.0, x=500.0, alpha=ALPHA)
    assert state.avg == 500.0
    assert state.var == 0.0
    assert state.std == 0.0


def test_ewma_warmup_matches_welford_mean_before_1_over_alpha():
    """Until n = 1/alpha, α_t = 1/n so the EWMA mean equals the lifetime mean."""
    welford = welford_update(0, 0.0, 0.0, 100.0)
    ewma = ewma_update(1, 0.0, 0.0, 100.0, ALPHA)
    amounts = [120.0, 80.0, 200.0, 90.0, 110.0, 95.0, 105.0, 300.0, 70.0]
    for x in amounts:
        welford = welford_update(welford.count, welford.avg, welford.m2, x)
        ewma = ewma_update(welford.count, ewma.avg, ewma.var, x, ALPHA)
        assert welford.count < (1 / ALPHA)
        assert ewma.avg == welford.avg


def test_ewma_locks_to_configured_alpha_after_warmup():
    avg, var = 0.0, 0.0
    for n, x in enumerate([10.0] * 20, start=1):
        state = ewma_update(n, avg, var, x, ALPHA)
        avg, var = state.avg, state.var
    # n=21: α_t must be 0.05, not 1/21. A jump of 90 from 10 moves the mean by 4.5.
    jumped = ewma_update(21, avg, var, 100.0, ALPHA)
    assert jumped.avg == pytest.approx(avg + ALPHA * (100.0 - avg))


def test_ewma_three_large_hits_move_mean_by_about_14_percent():
    """1 - (1-α)^3 ≈ 0.1426 of the gap from 0 to 100."""
    avg, var = 0.0, 0.0
    for n in range(1, 21):
        state = ewma_update(n, avg, var, 0.0, ALPHA)
        avg, var = state.avg, state.var
    for n in range(21, 24):
        state = ewma_update(n, avg, var, 100.0, ALPHA)
        avg, var = state.avg, state.var
    assert avg == pytest.approx((1.0 - (1.0 - ALPHA) ** 3) * 100.0)


def test_ewma_half_life_is_about_14_steps():
    """After warmup at 0, one 100, then 14 zeros: remaining jump is ~α*(0.95)^14."""
    avg, var = 0.0, 0.0
    for n in range(1, 21):
        state = ewma_update(n, avg, var, 0.0, ALPHA)
        avg, var = state.avg, state.var
    state = ewma_update(21, avg, var, 100.0, ALPHA)
    avg, var = state.avg, state.var
    assert avg == pytest.approx(5.0)
    for n in range(22, 36):
        state = ewma_update(n, avg, var, 0.0, ALPHA)
        avg, var = state.avg, state.var
    assert avg == pytest.approx(5.0 * ((1.0 - ALPHA) ** 14))


# --- Hypothesis: invariants over random streams ---


@given(xs=amount_streams)
@settings(max_examples=200)
def test_welford_mean_matches_batch_mean(xs):
    state = _fold_welford(xs)
    assert state.count == len(xs)
    assert state.avg == pytest.approx(statistics.mean(xs), rel=1e-9, abs=1e-6)


@given(xs=st.lists(naira, min_size=2, max_size=40))
@settings(max_examples=200)
def test_welford_std_matches_sample_std(xs):
    state = _fold_welford(xs)
    assert state.std == pytest.approx(statistics.stdev(xs), rel=1e-9, abs=1e-6)


@given(xs=st.lists(naira, min_size=1, max_size=WARMUP_MAX))
@settings(max_examples=200)
def test_ewma_warmup_mean_matches_welford_on_any_prefix(xs):
    welford = _fold_welford(xs)
    ewma = _fold_ewma(xs)
    assert welford.count < (1 / ALPHA)
    assert ewma.avg == pytest.approx(welford.avg, rel=1e-9, abs=1e-6)


@given(xs=amount_streams)
def test_welford_moments_stay_finite_and_non_negative(xs):
    state = _fold_welford(xs)
    assert math.isfinite(state.avg)
    assert math.isfinite(state.m2)
    assert math.isfinite(state.std)
    assert state.m2 >= 0.0
    assert state.std >= 0.0


@given(xs=amount_streams)
def test_ewma_moments_stay_finite_and_non_negative(xs):
    state = _fold_ewma(xs)
    assert math.isfinite(state.avg)
    assert math.isfinite(state.var)
    assert math.isfinite(state.std)
    assert state.var >= 0.0
    assert state.std >= 0.0


@given(xs=st.lists(naira, min_size=2, max_size=40))
def test_welford_count_increments_by_one(xs):
    state = welford_update(0, 0.0, 0.0, xs[0])
    for x in xs[1:]:
        nxt = welford_update(state.count, state.avg, state.m2, x)
        assert nxt.count == state.count + 1
        state = nxt


@given(old=naira, x=naira, n=st.integers(min_value=2, max_value=80), var=st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False))
def test_ewma_mean_is_between_old_avg_and_new_sample(old, x, n, var):
    """μ_new = (1-α_t)μ + α_t x, so it is a convex combination."""
    state = ewma_update(n, old, var, x, ALPHA)
    lo, hi = (old, x) if old <= x else (x, old)
    assert lo <= state.avg <= hi


@given(
    old=naira,
    recent=naira,
    history=st.integers(min_value=20, max_value=40),
    k=st.integers(min_value=15, max_value=30),
)
@settings(max_examples=100)
def test_ewma_tracks_recent_cluster_closer_than_welford(old, recent, history, k):
    """After living at `old` then switching to `recent`, EWMA follows the switch faster than Welford."""
    assume(abs(old - recent) > 50.0)
    stream = [old] * history + [recent] * k
    welford = _fold_welford(stream)
    ewma = _fold_ewma(stream)
    assert abs(ewma.avg - recent) < abs(welford.avg - recent)


@given(n=st.integers(max_value=0), x=naira)
def test_ewma_rejects_n_below_one(n, x):
    with pytest.raises(ValueError, match="n must be >= 1"):
        ewma_update(n, 0.0, 0.0, x, ALPHA)


@given(
    alpha=st.one_of(
        st.just(0.0),
        st.floats(max_value=-1e-9, min_value=-10.0, allow_nan=False, allow_infinity=False),
        st.floats(min_value=1.0000001, max_value=10.0, allow_nan=False, allow_infinity=False),
    ),
    x=naira,
)
def test_ewma_rejects_alpha_outside_unit_interval(alpha, x):
    with pytest.raises(ValueError, match="alpha must be in"):
        ewma_update(1, 0.0, 0.0, x, alpha)
