"""Feature-builder tests, focused on leakage.

The key property: a fight's features can depend only on what happened
*before* it. Removing future fights, or changing the fight's own stats, must
not change its row.
"""

from __future__ import annotations

import random
import sqlite3
import unittest

import numpy as np
import pandas as pd

from ufc_edge.data.store import SCHEMA
from ufc_edge.features.build import ELO_START, NUMERIC_FEATURES, assemble, build_fight_features, load_tables


def new_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    return conn


def add_fight(conn: sqlite3.Connection, fight_id: str, date: str, winner: str, loser: str, *,
              method: str = "DEC", duration: int = 900, outcome: str = "WIN", order: int = 0,
              w_stats: tuple = (30, 60, 2, 4, 1, 0, 120), l_stats: tuple = (20, 50, 0, 3, 0, 0, 30)) -> None:
    """Insert a fight with the winner listed first, as UFCStats does.

    stats tuple: (sig_landed, sig_att, td_landed, td_att, sub_att, kd, ctrl_sec)
    """
    event = f"e-{date}"
    conn.execute("INSERT OR IGNORE INTO events(event_id, name, date, is_complete, scraped_at) "
                 "VALUES (?,?,?,1,'x')", (event, event, date))
    for fid in (winner, loser):
        conn.execute("INSERT OR IGNORE INTO fighters(fighter_id, name, height_cm, reach_cm, stance, dob, scraped_at)"
                     " VALUES (?,?,180,185,'Orthodox','1990-01-01','x')", (fid, f"Fighter {fid}"))
    conn.execute(
        """INSERT INTO fights(fight_id, event_id, date, bout_order, fighter1_id, fighter2_id,
             fighter1_status, fighter2_status, winner_id, outcome, weight_class, title_bout, women,
             method, method_group, end_round, end_time_sec, scheduled_rounds, fight_duration_sec)
           VALUES (?,?,?,?,?,?,?,?,?,?,'Lightweight',0,0,?,?,?,?,3,?)""",
        (fight_id, event, date, order, winner, loser,
         "W" if outcome == "WIN" else "D", "L" if outcome == "WIN" else "D",
         winner if outcome == "WIN" else None, outcome, method, method,
         (duration - 1) // 300 + 1, (duration - 1) % 300 + 1, duration),
    )
    for fid, s in ((winner, w_stats), (loser, l_stats)):
        conn.execute(
            """INSERT INTO fight_stats(fight_id, fighter_id, round, sig_landed, sig_att, td_landed,
                 td_att, sub_att, kd, ctrl_sec, head_landed, distance_landed, clinch_landed, ground_landed)
               VALUES (?,?,0,?,?,?,?,?,?,?,?,?,0,0)""",
            (fight_id, fid, *s, s[0], s[0]),
        )


def random_db(seed: int = 7, n_fighters: int = 16, n_fights: int = 240) -> sqlite3.Connection:
    rng = random.Random(seed)
    conn = new_db()
    fighters = [f"f{i:02d}" for i in range(n_fighters)]
    day = pd.Timestamp("2015-01-03")
    for i in range(n_fights):
        if i % 4 == 0:
            day += pd.Timedelta(days=rng.randint(7, 21))
        a, b = rng.sample(fighters, 2)
        rs = lambda: (rng.randint(5, 80), rng.randint(80, 160), rng.randint(0, 4), rng.randint(4, 8),  # noqa: E731
                      rng.randint(0, 2), rng.randint(0, 1), rng.randint(0, 400))
        outcome = "DRAW" if rng.random() < 0.02 else "WIN"
        add_fight(conn, f"x{i:04d}", day.strftime("%Y-%m-%d"), a, b, order=i % 4,
                  method=rng.choice(["DEC", "KO_TKO", "SUB"]), duration=rng.randint(60, 900),
                  outcome=outcome, w_stats=rs(), l_stats=rs())
    return conn


def _numeric(df: pd.DataFrame) -> pd.DataFrame:
    assert df.columns.is_unique, "duplicate column names"
    cols = [c for c in df.columns if c.startswith(("a_", "b_", "diff_")) and df[c].dtype != object]
    return df.set_index("fight_id")[cols].sort_index()


class TestNoLeakage(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = random_db()
        self.addCleanup(self.conn.close)
        self.full = build_fight_features(self.conn)

    def test_future_fights_do_not_change_past_rows(self) -> None:
        fights, stats, fighters = load_tables(self.conn)
        for cutoff in ("2016-01-01", "2017-06-01", "2018-03-01"):
            past = fights[fights["date"] <= cutoff]
            truncated = assemble(past, stats[stats["fight_id"].isin(past["fight_id"])], fighters)
            expected = self.full[self.full["fight_id"].isin(past["fight_id"])]
            pd.testing.assert_frame_equal(_numeric(truncated), _numeric(expected))

    def test_fight_own_stats_do_not_affect_its_row(self) -> None:
        target = self.full["fight_id"].iloc[150]
        self.conn.execute("UPDATE fight_stats SET sig_landed = 999, kd = 9 WHERE fight_id = ?", (target,))
        self.conn.execute("UPDATE fights SET method_group = 'SUB', fight_duration_sec = 10 WHERE fight_id = ?",
                          (target,))
        after = build_fight_features(self.conn)
        pd.testing.assert_frame_equal(_numeric(after).loc[[target]], _numeric(self.full).loc[[target]])

    def test_corner_order_is_uninformative(self) -> None:
        # The source always lists the winner first. After re-orientation A should win about half the time.
        labelled = self.full["a_wins"].dropna()
        self.assertGreater(len(labelled), 200)
        self.assertTrue(0.40 < labelled.mean() < 0.60, labelled.mean())
        wins = self.full.dropna(subset=["a_wins"])
        winners = dict(self.conn.execute("SELECT fight_id, winner_id FROM fights WHERE outcome='WIN'").fetchall())
        for row in wins.itertuples():
            self.assertEqual(row.a_wins == 1.0, winners[row.fight_id] == row.a_id)

    def test_draws_have_no_label(self) -> None:
        draws = [r[0] for r in self.conn.execute("SELECT fight_id FROM fights WHERE outcome = 'DRAW'")]
        if draws:
            self.assertTrue(self.full.set_index("fight_id").loc[draws, "a_wins"].isna().all())

    def test_all_feature_columns_present(self) -> None:
        for c in NUMERIC_FEATURES:
            for p in ("a_", "b_", "diff_"):
                self.assertIn(p + c, self.full.columns)


class TestKnownValues(unittest.TestCase):
    """A tiny hand-checkable history."""

    def setUp(self) -> None:
        self.conn = new_db()
        self.addCleanup(self.conn.close)
        # X beats A (KO, 5 min), X beats B (decision, 15 min), C beats X, then X vs D.
        add_fight(self.conn, "f1", "2020-01-01", "X", "A", method="KO_TKO", duration=300,
                  w_stats=(30, 60, 1, 2, 0, 1, 60), l_stats=(10, 40, 0, 0, 0, 0, 0))
        add_fight(self.conn, "f2", "2020-04-01", "X", "B", method="DEC", duration=900,
                  w_stats=(60, 120, 2, 6, 1, 0, 120), l_stats=(30, 90, 1, 2, 0, 0, 30))
        add_fight(self.conn, "f3", "2020-08-01", "C", "X", method="SUB", duration=600,
                  w_stats=(20, 40, 3, 4, 2, 0, 300), l_stats=(10, 30, 0, 1, 0, 0, 0))
        add_fight(self.conn, "f4", "2021-01-01", "D", "X", method="DEC", duration=900)
        self.df = build_fight_features(self.conn).set_index("fight_id")

    def x(self, fight: str, feat: str) -> float:
        row = self.df.loc[fight]
        side = "a" if row["a_id"] == "X" else "b"
        return row[f"{side}_{feat}"]

    def test_debut_values(self) -> None:
        self.assertEqual(self.x("f1", "n_fights"), 0)
        self.assertEqual(self.x("f1", "win_rate"), 0.5)
        self.assertEqual(self.x("f1", "elo"), ELO_START)
        self.assertTrue(np.isnan(self.x("f1", "sig_landed_pm")))
        self.assertTrue(np.isnan(self.x("f1", "days_since_last")))

    def test_values_before_fourth_fight(self) -> None:
        self.assertEqual(self.x("f4", "n_fights"), 3)
        self.assertEqual((self.x("f4", "n_wins"), self.x("f4", "n_losses")), (2, 1))
        self.assertEqual(self.x("f4", "streak"), -1)
        self.assertAlmostEqual(self.x("f4", "win_rate"), 3 / 5)
        # 100 sig landed over 30 minutes; 60 absorbed.
        self.assertAlmostEqual(self.x("f4", "sig_landed_pm"), 100 / 30)
        self.assertAlmostEqual(self.x("f4", "sig_absorbed_pm"), 60 / 30)
        self.assertAlmostEqual(self.x("f4", "sig_acc"), 100 / 210)
        self.assertAlmostEqual(self.x("f4", "td_acc"), 3 / 9)
        self.assertAlmostEqual(self.x("f4", "td_def"), 1 - 4 / 6)
        self.assertAlmostEqual(self.x("f4", "ko_win_rate"), 1 / 3)
        self.assertAlmostEqual(self.x("f4", "sub_loss_rate"), 1 / 3)
        self.assertEqual(self.x("f4", "days_since_last"), 153)
        self.assertAlmostEqual(self.x("f4", "age"), (pd.Timestamp("2021-01-01") - pd.Timestamp("1990-01-01")).days / 365.25)

    def test_elo_moves_after_results_only(self) -> None:
        self.assertGreater(self.x("f2", "elo"), ELO_START)       # after one win
        self.assertGreater(self.x("f3", "elo"), self.x("f2", "elo"))
        self.assertLess(self.x("f4", "elo"), self.x("f3", "elo"))  # after the loss

    def test_upcoming_bout_uses_full_history(self) -> None:
        self.conn.execute(
            """INSERT INTO upcoming_bouts(event_id, event_name, date, bout_order, fighter1_id, fighter1_name,
                 fighter2_id, fighter2_name, weight_class, scraped_at)
               VALUES ('u1','UFC Next','2021-06-01',0,'X','Fighter X','C','Fighter C','Lightweight','x')""")
        df = build_fight_features(self.conn).set_index("fight_id")
        row = df.loc["upcoming:u1:0"]
        self.assertEqual(row["is_upcoming"], 1)
        self.assertTrue(np.isnan(row["a_wins"]))
        side = "a" if row["a_id"] == "X" else "b"
        self.assertEqual(row[f"{side}_n_fights"], 4)
        self.assertEqual(row[f"{side}_streak"], -2)


if __name__ == "__main__":
    unittest.main()
