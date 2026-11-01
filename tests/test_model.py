"""Model tests: symmetry, no training on the future, blend behaviour, upcoming-card matching."""

from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from test_features import add_fight, new_db

from ufc_edge.data.names import fuzzy_unique_match, same_fighter
from ufc_edge.data.odds_api import save_snapshot
from ufc_edge.features.build import build_fight_features
from ufc_edge.model import artifact
from ufc_edge.model.core import MarketBlend, StatsModel, feature_matrix, flip
from ufc_edge.model.upcoming import _cluster_cards, card_from_odds, predict_card
from ufc_edge.model.walkforward import betting_preview, walk_forward


def skill_db(seed: int = 3, n_fighters: int = 60, n_fights: int = 1400) -> sqlite3.Connection:
    """Synthetic history where a hidden 'skill' decides fights, so the stats carry real signal."""
    rng = random.Random(seed)
    conn = new_db()
    skill = {f"f{i:03d}": rng.gauss(0, 1) for i in range(n_fighters)}
    day = pd.Timestamp("2010-01-02")
    fighters = list(skill)
    for i in range(n_fights):
        if i % 10 == 0:
            day += pd.Timedelta(days=rng.randint(10, 20))
        a, b = rng.sample(fighters, 2)
        p_a = 1 / (1 + np.exp(-(skill[a] - skill[b])))
        w, l = (a, b) if rng.random() < p_a else (b, a)
        good = lambda s: (int(40 + 15 * s + rng.randint(0, 10)), 100, int(2 + s), 6, 0, 0, int(100 + 60 * s))  # noqa: E731
        add_fight(conn, f"x{i:05d}", day.strftime("%Y-%m-%d"), w, l, order=i % 10, duration=900,
                  w_stats=good(skill[w]), l_stats=good(skill[l]))
    return conn


class TestModelCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        conn = skill_db()
        cls.df = build_fight_features(conn)
        conn.close()
        cls.train = cls.df[cls.df["a_wins"].notna()]

    def test_symmetry(self) -> None:
        m = StatsModel().fit(self.train)
        sample = self.train.tail(50)
        swapped = sample.copy()
        X, Xf = feature_matrix(sample), flip(feature_matrix(sample))
        swapped[X.columns] = Xf.to_numpy()
        np.testing.assert_allclose(m.predict(sample), 1 - m.predict(swapped), atol=1e-9)

    def test_learns_real_signal(self) -> None:
        wf = walk_forward(self.df, first_test_year=2013, with_methods=False)
        p = wf.predictions
        acc = ((p["p_stats"] > 0.5) == (p["a_wins"] == 1)).mean()
        self.assertGreater(acc, 0.58)

    def test_walk_forward_never_uses_the_future(self) -> None:
        """Predictions for 2013-2014 must be identical whether or not later fights exist."""
        full = walk_forward(self.df, first_test_year=2013, with_methods=False).predictions
        cut = self.df[self.df["date"] < "2015-01-01"]
        part = walk_forward(cut, first_test_year=2013, with_methods=False).predictions
        a = full[full["year"] <= 2014].set_index("fight_id")[["p_stats", "p_logreg", "p_elo"]].sort_index()
        b = part.set_index("fight_id")[["p_stats", "p_logreg", "p_elo"]].sort_index()
        pd.testing.assert_frame_equal(a, b)


    def test_test_year_results_are_never_trained_on(self) -> None:
        """Flipping every 2014 result must not change the 2014 predictions."""
        base = walk_forward(self.df, first_test_year=2014, with_methods=False).predictions
        tampered = self.df.copy()
        is_2014 = (tampered["date"].dt.year == 2014) & tampered["a_wins"].notna()
        tampered.loc[is_2014, "a_wins"] = 1 - tampered.loc[is_2014, "a_wins"]
        after = walk_forward(tampered, first_test_year=2014, with_methods=False).predictions
        cols = ["p_stats", "p_logreg", "p_elo"]
        pd.testing.assert_frame_equal(
            base[base["year"] == 2014].set_index("fight_id")[cols].sort_index(),
            after[after["year"] == 2014].set_index("fight_id")[cols].sort_index())


class TestMarketBlend(unittest.TestCase):
    def test_ignores_noise_and_uses_signal(self) -> None:
        rng = np.random.default_rng(0)
        n = 6000
        truth = rng.uniform(0.15, 0.85, n)
        y = (rng.uniform(size=n) < truth).astype(float)
        market = np.clip(truth + rng.normal(0, 0.05, n), 0.02, 0.98)
        noise = rng.uniform(0.2, 0.8, n)
        b = MarketBlend().fit(market, noise, y)
        self.assertLess(abs(b.w_stats), 0.15)
        self.assertGreater(b.w_market, 0.8)
        # A stats model that knows something the market doesn't should earn weight.
        informative = np.clip(truth + rng.normal(0, 0.02, n), 0.02, 0.98)
        noisy_market = np.clip(truth + rng.normal(0, 0.15, n), 0.02, 0.98)
        b2 = MarketBlend().fit(noisy_market, informative, y)
        self.assertGreater(b2.w_stats, 0.5)

    def test_symmetric(self) -> None:
        b = MarketBlend()
        b.w_market, b.w_stats = 0.9, 0.3
        p = b.predict(np.array([0.7]), np.array([0.6]))
        q = b.predict(np.array([0.3]), np.array([0.4]))
        self.assertAlmostEqual(float(p[0]), 1 - float(q[0]))


class TestBettingPreview(unittest.TestCase):
    def test_flat_stake_accounting(self) -> None:
        preds = pd.DataFrame({
            "p_final": [0.6, 0.6, 0.3], "a_odds": [2.0, 2.0, 1.5], "b_odds": [1.9, 1.9, 2.9],
            "a_wins": [1.0, 0.0, 0.0], "year": [2020, 2020, 2021],
        })
        r = betting_preview(preds, "p_final")
        # Fight 1: back A at 2.0 (EV +20%) and win +1. Fight 2: back A and lose -1.
        # Fight 3: B has 70% at 2.9 (EV +103%) and wins +1.9.
        self.assertEqual(r["bets"], 3)
        self.assertAlmostEqual(r["roi"], (1 - 1 + 1.9) / 3)


class TestNames(unittest.TestCase):
    def test_nicknames_and_fuzzy(self) -> None:
        self.assertTrue(same_fighter("Joe Pyfer", "Joseph Pyfer"))
        self.assertTrue(same_fighter("Zach Reese", "Zachary Reese"))
        people = [("1", "JeongYeong Lee"), ("2", "JungHyun Lee"), ("3", "James Lee"), ("4", "Rafael Cerqueira")]
        self.assertEqual(fuzzy_unique_match("Jeongyoung Lee", people), "1")
        self.assertEqual(fuzzy_unique_match("Rafael Cerquiera", people), "4")
        self.assertIsNone(fuzzy_unique_match("J Lee", people))
        self.assertIsNone(fuzzy_unique_match("Bob Smith", people))


def _event(eid: str, t: str, home: str, away: str, p_home: float) -> dict:
    return {"id": eid, "commence_time": t, "home_team": home, "away_team": away, "bookmakers": [
        {"key": f"bk{i}", "title": f"Book {i}", "last_update": t, "markets": [{"key": "h2h", "outcomes": [
            {"name": home, "price": round(1 / (p_home * 1.04) + i * 0.01, 2)},
            {"name": away, "price": round(1 / ((1 - p_home) * 1.04) + i * 0.01, 2)}]}]} for i in range(3)]}


class TestUpcomingCard(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = new_db()
        self.addCleanup(self.conn.close)
        add_fight(self.conn, "h1", "2020-01-01", "AA", "BB")
        add_fight(self.conn, "h2", "2020-02-01", "CC", "DD")
        self.conn.execute("UPDATE fighters SET name='Joseph Alpha' WHERE fighter_id='AA'")
        self.conn.execute("UPDATE fighters SET name='Ben Bravo' WHERE fighter_id='BB'")
        self.conn.execute("UPDATE fighters SET name='Cara Charlie' WHERE fighter_id='CC'")
        self.conn.execute("UPDATE fighters SET name='Dana Delta' WHERE fighter_id='DD'")
        self.conn.execute("INSERT INTO fighters(fighter_id, name, scraped_at) VALUES ('EE', 'Eve Echo', 'x')")
        future = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=5)).floor("h")
        t1 = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = (future - pd.Timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        far = (future + pd.Timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [
            _event("e1", t1, "Joe Alpha", "Ben Bravo", 0.6),        # nickname match
            _event("e2", t0, "Cara Charlie", "Eve Echo", 0.7),       # same card, debutant with profile
            _event("e3", far, "Other Guy", "Someone Else", 0.5),     # other promotion: dropped
            _event("e4", far, "Dana Delta", "Stranger Person", 0.5), # lone known name on another card: dropped
        ]
        save_snapshot(self.conn, events, pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_card_matching_and_filtering(self) -> None:
        card = card_from_odds(self.conn)
        self.assertEqual([f.api_event_id for f in card], ["e2", "e1"])
        e1 = next(f for f in card if f.api_event_id == "e1")
        self.assertEqual((e1.fighter1_id, e1.fighter2_id), ("AA", "BB"))
        self.assertAlmostEqual(e1.consensus_p1, 0.6, delta=0.02)
        self.assertEqual(e1.books, 3)

    def test_cluster(self) -> None:
        ts = [pd.Timestamp("2026-11-01 00:00"), pd.Timestamp("2026-10-31 21:00"), pd.Timestamp("2026-11-08")]
        self.assertEqual(_cluster_cards(ts), [0, 0, 1])

    def test_predict_card_end_to_end(self) -> None:
        df = build_fight_features(skill_db(n_fights=1200))
        wf = walk_forward(df.assign(market_p_a=np.nan), first_test_year=2013, with_methods=False)
        bundle = artifact.train_final(df, wf)
        with tempfile.TemporaryDirectory() as d:
            artifact.save(bundle, Path(d) / "m.pkl")
            bundle = artifact.load(Path(d) / "m.pkl")
        preds = predict_card(self.conn, bundle)
        self.assertEqual(len(preds), 2)
        row = preds[preds["fighter_1"] == "Joe Alpha"].iloc[0]
        self.assertTrue(0 < row["p_model_1"] < 1)
        self.assertAlmostEqual(sum(row[f"f{s}_{m}"] for s in (1, 2) for m in ("KO_TKO", "SUB", "DEC")), 1, places=6)
        debut = preds[preds["fighter_2"] == "Eve Echo"].iloc[0]
        self.assertIn("UFC debut", debut["warnings"])


if __name__ == "__main__":
    unittest.main()
