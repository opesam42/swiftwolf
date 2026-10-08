"""Pure Welford / EWMA updates — no database."""

import math

import pytest

from src.profile.stats import ewma_update, welford_update

ALPHA = 0.05

# TODO - USE HYPOTHESIS TESTING

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
