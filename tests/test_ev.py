"""Tests for odds, de-vig, expected value and stake sizing maths."""

from __future__ import annotations

import unittest

from ufc_edge import ev


class TestOddsMaths(unittest.TestCase):
    def test_american_to_decimal(self) -> None:
        self.assertAlmostEqual(ev.american_to_decimal(-150), 1.6667, places=4)
        self.assertAlmostEqual(ev.american_to_decimal(130), 2.30)
        with self.assertRaises(ValueError):
            ev.american_to_decimal(50)

    def test_overround(self) -> None:
        self.assertAlmostEqual(ev.overround([1.91, 1.91]), 0.04712, places=4)

    def test_devig_sums_to_one(self) -> None:
        for method in (ev.devig_multiplicative, ev.devig_power):
            probs = method([1.40, 3.00])
            self.assertAlmostEqual(sum(probs), 1.0, places=9)

    def test_power_devig_shades_longshot(self) -> None:
        mult = ev.devig_multiplicative([1.25, 4.20])
        power = ev.devig_power([1.25, 4.20])
        self.assertLess(power[1], mult[1])  # more margin removed from the underdog

    def test_expected_value_formula(self) -> None:
        # Spec formula: EV = p*(odds-1) - (1-p)
        self.assertAlmostEqual(ev.expected_value(0.5, 2.2), 0.10)
        self.assertAlmostEqual(ev.expected_value(0.5, 2.0), 0.0)
        self.assertAlmostEqual(ev.expected_value(0.5, 2.2, commission=0.05), 0.07)

    def test_high_probability_is_not_value(self) -> None:
        """A 75% favourite at 1.25 is a likely winner but a losing bet."""
        self.assertLess(ev.expected_value(0.75, 1.25), 0)

    def test_kelly(self) -> None:
        self.assertAlmostEqual(ev.kelly_fraction(0.5, 3.0), 0.25)
        self.assertEqual(ev.kelly_fraction(0.3, 2.0), 0.0)


class TestStakeRecommendation(unittest.TestCase):
    base = dict(
        capital_in_play_pence=4000, kelly_multiplier=0.25, model_weight=0.5,
        min_edge=0.03, max_stake_fraction=0.05, min_stake_pence=10,
    )

    def test_no_edge_no_bet(self) -> None:
        r = ev.recommend_stake(p_model=0.40, p_market=0.40, decimal_odds=2.40, **self.base)
        self.assertFalse(r.qualifies)
        self.assertEqual(r.stake_pence, 0)

    def test_blending_reduces_overconfidence(self) -> None:
        r = ev.recommend_stake(p_model=0.60, p_market=0.40, decimal_odds=2.40, **self.base)
        self.assertAlmostEqual(r.p_blended, 0.50)
        self.assertTrue(r.qualifies)

    def test_stake_is_capped(self) -> None:
        r = ev.recommend_stake(p_model=0.90, p_market=0.60, decimal_odds=2.50, **self.base)
        self.assertEqual(r.stake_pence, 200)  # 5% of £40


if __name__ == "__main__":
    unittest.main()
