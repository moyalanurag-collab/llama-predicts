"""Odds, probability and expected-value maths.

Everything here is a pure function with no I/O, so it is easy to test and
reuse in the backtester, paper trading and the live recommender.

Key ideas
---------
* Bookmaker odds include a margin (the "overround"), so raw implied
  probabilities sum to more than 1. ``devig_*`` removes the margin to get
  the market's fair probability estimate.
* The model's probability is never trusted as exact. ``blend_probability``
  pulls it towards the market. The market is a strong forecaster, and an
  edge that survives blending is more credible.
* Staking uses fractional Kelly on the *blended* probability, capped by the
  risk limits. The risk engine re-checks the cap independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Sequence


def implied_probability(decimal_odds: float) -> float:
    """Raw implied probability of decimal odds (includes the bookmaker margin)."""
    if decimal_odds <= 1:
        raise ValueError("Decimal odds must be greater than 1.")
    return 1.0 / decimal_odds


def american_to_decimal(american: float) -> float:
    """Convert American moneyline odds (e.g. -150, +130) to decimal odds."""
    if american == 0 or -100 < american < 100:
        raise ValueError("American odds must be <= -100 or >= +100.")
    return 1 + (american / 100 if american > 0 else 100 / -american)


def overround(odds: Sequence[float]) -> float:
    """Bookmaker margin of a complete market, e.g. 0.045 for a 4.5% book."""
    return sum(implied_probability(o) for o in odds) - 1.0


def devig_multiplicative(odds: Sequence[float]) -> list[float]:
    """Remove margin by scaling implied probabilities to sum to 1."""
    raw = [implied_probability(o) for o in odds]
    total = sum(raw)
    return [p / total for p in raw]


def devig_power(odds: Sequence[float], tol: float = 1e-12) -> list[float]:
    """Remove margin with the power method: find k such that sum(p_i ** k) = 1.

    Unlike the multiplicative method, this puts more of the margin on the
    longshot, which matches the well-documented favourite-longshot bias.
    """
    raw = [implied_probability(o) for o in odds]
    if abs(sum(raw) - 1.0) < tol:
        return raw
    lo, hi = 0.5, 3.0
    for _ in range(200):
        k = (lo + hi) / 2
        s = sum(p**k for p in raw)
        if s > 1:
            lo = k
        else:
            hi = k
        if hi - lo < tol:
            break
    k = (lo + hi) / 2
    probs = [p**k for p in raw]
    total = sum(probs)
    return [p / total for p in probs]


def expected_value(p: float, decimal_odds: float, commission: float = 0.0) -> float:
    """Expected profit per £1 staked: EV = p*(odds-1)*(1-commission) - (1-p).

    ``commission`` is the share of net winnings taken by an exchange
    (e.g. 0.05 for 5%). It is 0 for a fixed-odds bookmaker.
    """
    if not 0 <= p <= 1:
        raise ValueError("Probability must be in [0, 1].")
    if decimal_odds <= 1:
        raise ValueError("Decimal odds must be greater than 1.")
    return p * (decimal_odds - 1) * (1 - commission) - (1 - p)


def fair_odds(p: float) -> float:
    """Decimal odds at which a bet with win probability p has zero EV."""
    if not 0 < p <= 1:
        raise ValueError("Probability must be in (0, 1].")
    return 1.0 / p


def blend_probability(p_model: float, p_market: float, model_weight: float) -> float:
    """Shrink the model's probability towards the market's fair probability."""
    if not 0 <= model_weight <= 1:
        raise ValueError("model_weight must be in [0, 1].")
    return model_weight * p_model + (1 - model_weight) * p_market


def kelly_fraction(p: float, decimal_odds: float, commission: float = 0.0) -> float:
    """Full-Kelly fraction of bankroll to stake (0 if the bet has no edge)."""
    b = (decimal_odds - 1) * (1 - commission)
    if b <= 0:
        return 0.0
    f = (b * p - (1 - p)) / b
    return max(0.0, f)


@dataclass(frozen=True)
class StakeRecommendation:
    p_model: float
    p_market: float
    p_blended: float
    decimal_odds: float
    ev_per_pound: float
    kelly_full: float
    stake_pence: int
    qualifies: bool
    reason: str


def recommend_stake(
    *,
    p_model: float,
    p_market: float,
    decimal_odds: float,
    capital_in_play_pence: int,
    kelly_multiplier: float,
    model_weight: float,
    min_edge: float,
    max_stake_fraction: float,
    min_stake_pence: int,
    commission: float = 0.0,
) -> StakeRecommendation:
    """Size a stake from the blended probability. Returns stake 0 if it doesn't qualify.

    This is the *strategy's* suggestion. The risk engine still validates it
    independently before anything is recorded.
    """
    p = blend_probability(p_model, p_market, model_weight)
    ev = expected_value(p, decimal_odds, commission)
    k = kelly_fraction(p, decimal_odds, commission)

    def result(stake: int, ok: bool, why: str) -> StakeRecommendation:
        return StakeRecommendation(p_model, p_market, p, decimal_odds, ev, k, stake, ok, why)

    if ev < min_edge:
        return result(0, False, f"edge {ev:.3f} below minimum {min_edge:.3f}")
    raw = Decimal(str(k * kelly_multiplier)) * capital_in_play_pence
    cap = Decimal(str(max_stake_fraction)) * capital_in_play_pence
    stake = int(min(raw, cap).to_integral_value(rounding=ROUND_FLOOR))
    if stake < min_stake_pence:
        return result(0, False, f"stake {stake}p below minimum {min_stake_pence}p")
    return result(stake, True, "qualifies")
