"""Historical odds loader tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ufc_edge.data.kaggle_odds import load_kaggle_odds
from ufc_edge.data.store import StatsStore
from ufc_edge.data.ufcstats_csv import load_csvs
from ufc_edge.features.build import attach_market_odds, build_fight_features

FIX = Path(__file__).parent / "fixtures"


class TestKaggleOdds(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StatsStore(Path(tmp.name) / "ufc.db")
        self.addCleanup(self.store.close)
        load_csvs(self.store, FIX / "ufcstats_csv")
        self.c = self.store.conn
        self.r = load_kaggle_odds(self.c, FIX / "kaggle" / "ufc-master.csv")

    def odds(self, fight_id: str, fighter_id: str) -> float:
        return self.c.execute("SELECT decimal_odds FROM historical_odds WHERE fight_id=? AND fighter_id=?",
                              (fight_id, fighter_id)).fetchone()[0]

    def test_report(self) -> None:
        self.assertEqual((self.r.rows, self.r.matched, self.r.no_odds), (5, 3, 1))
        self.assertEqual(self.r.unmatched, ["2024-04-13 Nobody Atall vs Someone Else"])
        self.assertEqual(len(self.r.matched_loosely), 1)  # "Alan Abel" -> "Alan Able"

    def test_corner_swap_and_conversion(self) -> None:
        # Kaggle lists Dee in red; our database lists Cole first. Odds must follow the fighter.
        self.assertAlmostEqual(self.odds("ft1", "fD"), 2.5)
        self.assertAlmostEqual(self.odds("ft1", "fC"), 1.5556, places=4)

    def test_date_off_by_one_and_namesake(self) -> None:
        self.assertAlmostEqual(self.odds("ft2", "fB"), 1.4)
        self.assertAlmostEqual(self.odds("ft2", "bsFly"), 3.0)

    def test_method_props_kept(self) -> None:
        row = self.c.execute("SELECT dec_odds, ko_odds FROM historical_odds WHERE fight_id='ft1' AND fighter_id='fC'"
                             ).fetchone()
        self.assertEqual(tuple(row), (120.0, 350.0))

    def test_reload_replaces(self) -> None:
        load_kaggle_odds(self.c, FIX / "kaggle" / "ufc-master.csv")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM historical_odds").fetchone()[0], 6)

    def test_market_probability_attached(self) -> None:
        df = attach_market_odds(build_fight_features(self.c), self.c).set_index("fight_id")
        row = df.loc["ft1"]
        p_cole = row["market_p_a"] if row["a_id"] == "fC" else 1 - row["market_p_a"]
        self.assertTrue(0.55 < p_cole < 0.65)
        self.assertAlmostEqual(row["overround"], 1 / 2.5 + 1 / 1.5556 - 1, places=3)
        self.assertTrue(df.loc[["ft3", "ft5"], "market_p_a"].isna().all())


if __name__ == "__main__":
    unittest.main()
