"""Beta-posterior reliability tests."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gravitar.feedback.reliability import (
    ALPHA_PRIOR,
    BETA_PRIOR,
    reliability,
    reliability_multiplier,
)

# --- reliability(): the Beta(alpha, beta) posterior mean ---


def test_zero_observations_centers_on_the_weak_prior() -> None:
    assert reliability(0, 0) == pytest.approx(0.5)


def test_all_confirmations_moves_above_the_prior() -> None:
    assert reliability(5, 0) > 0.5


def test_all_dismissals_moves_below_the_prior() -> None:
    assert reliability(0, 5) < 0.5


def test_matches_hand_computed_posterior() -> None:
    # (confirm + alpha) / (confirm + dismiss + alpha + beta), alpha=beta=2 by default
    assert reliability(3, 1) == pytest.approx((3 + 2) / (3 + 1 + 2 + 2))


def test_custom_priors_are_honored() -> None:
    assert reliability(0, 0, alpha=1.0, beta=3.0) == pytest.approx(0.25)


# --- Property tests: convergence direction and range (Section 6) ---


@given(st.integers(min_value=0, max_value=1000))
def test_reliability_increases_toward_one_with_more_confirmations(n: int) -> None:
    fewer = reliability(n, 0)
    more = reliability(n + 1, 0)
    assert more >= fewer


@given(st.integers(min_value=0, max_value=1000))
def test_reliability_decreases_toward_zero_with_more_dismissals(n: int) -> None:
    fewer = reliability(0, n)
    more = reliability(0, n + 1)
    assert more <= fewer


@given(st.integers(min_value=0, max_value=10_000))
def test_reliability_all_confirmations_matches_exact_posterior_formula(n: int) -> None:
    assert reliability(n, 0) == pytest.approx((n + ALPHA_PRIOR) / (n + ALPHA_PRIOR + BETA_PRIOR))


@given(st.integers(min_value=0, max_value=10_000))
def test_reliability_all_dismissals_matches_exact_posterior_formula(n: int) -> None:
    assert reliability(0, n) == pytest.approx(ALPHA_PRIOR / (n + ALPHA_PRIOR + BETA_PRIOR))


def test_reliability_converges_close_to_one_with_many_confirmations() -> None:
    assert reliability(10_000, 0) > 0.999


def test_reliability_converges_close_to_zero_with_many_dismissals() -> None:
    assert reliability(0, 10_000) < 0.001


@given(st.integers(min_value=0, max_value=10_000), st.integers(min_value=0, max_value=10_000))
def test_reliability_always_stays_in_unit_range(confirm: int, dismiss: int) -> None:
    assert 0.0 <= reliability(confirm, dismiss) <= 1.0


# --- reliability_multiplier(): the cold-start gate ---


def test_multiplier_is_neutral_with_zero_observations() -> None:
    assert reliability_multiplier(0, 0) == 1.0


def test_multiplier_applies_the_posterior_once_a_pattern_has_any_observation() -> None:
    assert reliability_multiplier(1, 0) == pytest.approx(reliability(1, 0))
    assert reliability_multiplier(0, 1) == pytest.approx(reliability(0, 1))


@given(st.integers(min_value=0, max_value=10_000), st.integers(min_value=0, max_value=10_000))
def test_multiplier_always_stays_in_unit_range(confirm: int, dismiss: int) -> None:
    assert 0.0 <= reliability_multiplier(confirm, dismiss) <= 1.0


def test_default_priors_are_a_weak_prior_centered_at_half() -> None:
    assert ALPHA_PRIOR == BETA_PRIOR
    assert reliability(0, 0, alpha=ALPHA_PRIOR, beta=BETA_PRIOR) == pytest.approx(0.5)
