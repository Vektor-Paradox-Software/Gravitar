"""Beta-posterior pattern reliability.

A raw confirm/dismiss ratio is unstable with few observations — one dismissal out
of one observation reads as 0% reliable, which isn't a meaningful signal yet. The
Beta-distribution posterior mean handles small-sample uncertainty correctly while
converging to the empirical rate as data accumulates: `alpha`/`beta` act as a weak
prior (default 2/2, centered at 0.5) that repeated confirmations or dismissals
overwhelm as `confirm_count`/`dismiss_count` grow.

`reliability_multiplier` is the function actually wired into the confidence
formula (recalibrate.py); it's a thin, deliberate policy wrapper around
`reliability`, not an equivalent implementation of it — see its own docstring.
"""

from __future__ import annotations

ALPHA_PRIOR = 2.0
BETA_PRIOR = 2.0


def reliability(
    confirm_count: int, dismiss_count: int, *, alpha: float = ALPHA_PRIOR, beta: float = BETA_PRIOR
) -> float:
    """Beta(alpha, beta) posterior mean given confirm_count successes and
    dismiss_count failures. Always in [0, 1].
    """
    return (confirm_count + alpha) / (confirm_count + dismiss_count + alpha + beta)


def reliability_multiplier(confirm_count: int, dismiss_count: int) -> float:
    """The `historical_reliability(pattern_id)` multiplier for the confidence
    formula.

    Local-first (no cross-user aggregation), so every pattern starts at zero
    observations in every repo. Applying `reliability`'s posterior mean
    unconditionally would mean every finding's confidence is cut roughly in half
    by default — forever, in any repo that has never used `gravitar feedback`.
    This returns 1.0 (no adjustment) until a pattern has at least one recorded
    outcome; a future cross-repo/cloud-pooled observation count is the seam this
    gate is designed to be superseded by, not a workaround for it.
    """
    if confirm_count + dismiss_count == 0:
        return 1.0
    return reliability(confirm_count, dismiss_count)
