"""CSV history loader tests (offline, small hand-made files)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ufc_edge.data.http import FetchError, Fetcher, _looks_like_bot_challenge
from ufc_edge.data.store import StatsStore
from ufc_edge.data.ufcstats_csv import load_csvs
from ufc_edge.features.build import build_fight_features

FIX = Path(__file__).parent / "fixtures" / "ufcstats_csv"


class TestCsvLoader(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StatsStore(Path(self.tmp.name) / "ufc.db")
        self.addCleanup(self.store.close)
        self.report = load_csvs(self.store, FIX)
        self.c = self.store.conn

    def fight(self, fight_id: str) -> sqlite3.Row:
        return self.c.execute("SELECT * FROM fights WHERE fight_id = ?", (fight_id,)).fetchone()

    def test_counts_and_skips(self) -> None:
        r = self.report
        self.assertEqual((r.events, r.fights, r.duplicates), (2, 5, 1))
        self.assertEqual(len(r.skipped), 1)
        self.assertIn("Road to UFC", r.skipped[0])
        self.assertEqual(r.newest_event, "2025-09-13")

    def test_results_parsed(self) -> None:
        f = self.fight("ft1")
        self.assertEqual((f["winner_id"], f["outcome"], f["weight_class"], f["title_bout"], f["women"]),
                         ("fC", "WIN", "Women's Strawweight", 1, 1))
        self.assertEqual((f["method_group"], f["scheduled_rounds"], f["fight_duration_sec"], f["bout_order"]),
                         ("DEC", 5, 1500, 0))
        self.assertEqual(self.fight("ft2")["winner_id"], "fB")      # L/W: second-listed fighter won
        self.assertEqual(self.fight("ft3")["outcome"], "DRAW")
        self.assertEqual(self.fight("ft5")["outcome"], "NC")

    def test_renamed_event_matched(self) -> None:
        self.assertEqual(self.fight("ft4")["event_id"], "ev2")

    def test_namesakes_told_apart_by_weight(self) -> None:
        self.assertEqual(self.fight("ft2")["fighter1_id"], "bsFly")  # flyweight bout
        self.assertEqual(self.fight("ft5")["fighter1_id"], "bsMid")  # middleweight bout
        self.assertEqual(self.report.namesakes_resolved, {"Bruno Silva": 2})

    def test_spelling_variant_and_placeholder(self) -> None:
        f = self.fight("ft3")
        self.assertEqual(f["fighter1_id"], "wca")
        self.assertEqual(f["fighter2_id"], "name:zed newman")
        self.assertIn("Waldo Cortes Acosta", self.report.matched_normalised)
        self.assertEqual(self.report.placeholders, {"Zed Newman"})

    def test_round_stats_and_totals(self) -> None:
        rows = {r["round"]: r for r in self.c.execute(
            "SELECT * FROM fight_stats WHERE fight_id='ft1' AND fighter_id='fC'")}
        self.assertEqual(sorted(rows), [0, 1, 2])
        tot = rows[0]
        self.assertEqual((tot["sig_landed"], tot["sig_att"], tot["kd"], tot["ctrl_sec"], tot["sub_att"]),
                         (25, 45, 1, 90, 1))
        dee = self.c.execute("SELECT ctrl_sec, td_att FROM fight_stats "
                             "WHERE fight_id='ft1' AND fighter_id='fD' AND round=0").fetchone()
        self.assertEqual((dee["ctrl_sec"], dee["td_att"]), (None, 0))

    def test_reload_is_idempotent(self) -> None:
        load_csvs(self.store, FIX)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM fights").fetchone()[0], 5)

    def test_features_build_from_loaded_data(self) -> None:
        df = build_fight_features(self.c)
        self.assertEqual(len(df), 5)
        self.assertTrue(df["date"].is_monotonic_increasing)


class TestBotChallenge(unittest.TestCase):
    PAGE = ("<!doctype html><html><head><title>Loading…</title></head><body><p>Checking your browser…</p>"
            "<noscript>This site requires JavaScript.</noscript><script>var K=[1,2];</script></body></html>")

    def test_detected(self) -> None:
        self.assertTrue(_looks_like_bot_challenge(self.PAGE))
        self.assertFalse(_looks_like_bot_challenge("<html><table>real data</table></html>"))

    def test_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Fetcher(d, min_interval=0)
            f._download = lambda url: self.PAGE  # type: ignore[method-assign]
            with self.assertRaises(FetchError):
                f.get("http://example.com/x", "x.html")
            self.assertFalse((Path(d) / "x.html").exists())


if __name__ == "__main__":
    unittest.main()
