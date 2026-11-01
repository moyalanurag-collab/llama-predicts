"""Stage 4: betting plan, dry run, paper trading, settlement and backtest."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from helpers import Clock, auth_for_bankroll
from test_features import add_fight, new_db

from ufc_edge.backtest import run_backtest
from ufc_edge.config import RiskConfig
from ufc_edge.data.odds_api import save_snapshot
from ufc_edge.errors import UfcEdgeError
from ufc_edge.ev import recommend_stake
from ufc_edge.ledger import BankrollService
from ufc_edge.settlement import settle_open_bets
from ufc_edge.strategy import build_plan, dry_run, record_paper_bets

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
FIGHT = NOW + timedelta(hours=6)


def predictions(captured: datetime = NOW) -> pd.DataFrame:
    """Two fights: value on Bravo (fight 1); nothing in fight 2."""
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return pd.DataFrame([
        {"commence_time": iso(FIGHT), "api_event_id": "ev1", "fighter_1": "Alpha One", "fighter_2": "Bravo Two",
         "fighter_1_id": "AA", "fighter_2_id": "BB", "p_model_1": 0.48, "p_market_1": 0.55,
         "best_odds_1": 1.75, "best_odds_2": 2.40, "best_book_1": "Sky Bet", "best_book_2": "Betfair",
         "odds_captured_at": iso(captured)},
        {"commence_time": iso(FIGHT + timedelta(minutes=30)), "api_event_id": "ev2", "fighter_1": "Cara Three",
         "fighter_2": "Dana Four", "fighter_1_id": "CC", "fighter_2_id": "DD", "p_model_1": 0.60,
         "p_market_1": 0.60, "best_odds_1": 1.60, "best_odds_2": 2.40, "best_book_1": "Betway",
         "best_book_2": "Betway", "odds_captured_at": iso(captured)},
    ])


class Stage4Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = Clock(NOW)
        self.svc = BankrollService(self.tmp / "paper.db", "paper", clock=self.clock)
        self.addCleanup(self.svc.close)
        self.svc.initialise_bankroll(4000, auth_for_bankroll(4000))


class TestPlan(Stage4Case):
    def test_plan_finds_the_value_side(self) -> None:
        plan = build_plan(predictions(), self.svc)
        self.assertEqual([b.fighter_name for b in plan], ["Bravo Two"])
        b = plan[0]
        cfg = RiskConfig()
        rec = recommend_stake(p_model=0.52, p_market=0.45, decimal_odds=2.40, capital_in_play_pence=4000,
                              kelly_multiplier=cfg.kelly_fraction, model_weight=cfg.model_weight,
                              min_edge=cfg.min_edge, max_stake_fraction=cfg.max_stake_fraction,
                              min_stake_pence=cfg.min_stake_pence)
        self.assertEqual(b.proposal.stake_pence, rec.stake_pence)
        self.assertEqual(b.proposal.event_id, f"card:{FIGHT:%Y-%m-%d}")
        self.assertEqual(b.opponent_name, "Alpha One")

    def test_dry_run_never_touches_the_ledger(self) -> None:
        plan = dry_run(self.svc, build_plan(predictions(), self.svc))
        self.assertEqual(plan[0].status, "approved")
        self.assertEqual(self.svc.balances().open_stakes_pence, 0)
        self.assertEqual(self.svc.conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0], 0)

    def test_stale_odds_rejected_in_plan(self) -> None:
        plan = dry_run(self.svc, build_plan(predictions(captured=NOW - timedelta(hours=3)), self.svc))
        self.assertEqual(plan[0].status, "rejected")
        self.assertTrue(any(r.startswith("STALE_ODDS") for r in plan[0].reasons))

    def test_paper_recording_is_idempotent(self) -> None:
        first = record_paper_bets(self.svc, build_plan(predictions(), self.svc))
        self.assertEqual(first[0].status, "recorded")
        again = record_paper_bets(self.svc, build_plan(predictions(), self.svc))
        self.assertEqual(again[0].status, "already recorded")
        self.assertEqual(self.svc.conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0], 1)
        ctx = self.svc.conn.execute("SELECT fighter_id, opponent_name FROM bet_context").fetchone()
        self.assertEqual(tuple(ctx), ("BB", "Alpha One"))

    def test_live_mode_never_auto_records(self) -> None:
        live = BankrollService(self.tmp / "live.db", "live", clock=self.clock)
        self.addCleanup(live.close)
        live.initialise_bankroll(4000, auth_for_bankroll(4000))
        with self.assertRaises(UfcEdgeError):
            record_paper_bets(live, build_plan(predictions(), live))


class TestSettlement(Stage4Case):
    def setUp(self) -> None:
        super().setUp()
        record_paper_bets(self.svc, build_plan(predictions(), self.svc))
        self.stats = new_db()
        self.addCleanup(self.stats.close)
        save_snapshot(self.stats, [{"id": "ev1", "commence_time": FIGHT.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "home_team": "Alpha One", "away_team": "Bravo Two", "bookmakers": [
            {"key": "b", "title": "B", "markets": [{"key": "h2h", "last_update": "x", "outcomes": [
                {"name": "Alpha One", "price": 1.80}, {"name": "Bravo Two", "price": 2.20}]}]}]}],
            (FIGHT - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_waits_until_result_exists(self) -> None:
        self.clock.now = FIGHT + timedelta(hours=3)
        rep = settle_open_bets(self.svc, self.stats)
        self.assertEqual(rep.settled, [])
        self.assertEqual(len(rep.waiting), 1)

    def test_win_settles_with_clv(self) -> None:
        add_fight(self.stats, "r1", f"{FIGHT:%Y-%m-%d}", "BB", "AA")
        self.clock.now = FIGHT + timedelta(hours=3)
        rep = settle_open_bets(self.svc, self.stats)
        self.assertEqual(rep.settled, ["Bravo Two vs Alpha One: WON"])
        clv = self.svc.conn.execute("SELECT closing_odds, clv FROM bet_context").fetchone()
        self.assertAlmostEqual(clv[0], 2.20)
        self.assertAlmostEqual(clv[1], 2.40 / 2.20 - 1)
        self.assertGreater(self.svc.balances().active_pence, 4000 - 1)

    def test_no_contest_is_void(self) -> None:
        add_fight(self.stats, "r1", f"{FIGHT:%Y-%m-%d}", "AA", "BB", outcome="DRAW")
        self.stats.execute("UPDATE fights SET outcome='NC' WHERE fight_id='r1'")
        self.clock.now = FIGHT + timedelta(hours=3)
        settle_open_bets(self.svc, self.stats)
        self.assertEqual(self.svc.balances().active_pence, 4000)


class TestBacktest(unittest.TestCase):
    def frame(self, n_events: int, a_wins: float | None = None, seed: int = 0, p: float = 0.55) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        rows = []
        for e in range(n_events):
            day = pd.Timestamp("2015-01-03") + pd.Timedelta(days=7 * e)
            for f in range(3):
                rows.append({"fight_id": f"f{e}_{f}", "event_id": f"e{e}", "date": day, "a_name": f"A{e}{f}",
                             "b_name": f"B{e}{f}", "market_p_a": 0.45, "p_final": p, "a_odds": 2.15, "b_odds": 1.75,
                             "a_wins": float(rng.random() < p) if a_wins is None else a_wins})
        return pd.DataFrame(rows)

    def test_runs_through_real_ledger(self) -> None:
        res = run_backtest(self.frame(30), RiskConfig())
        s = res["summary"]
        self.assertGreater(s["bets"], 0)
        self.assertLessEqual(s["bets"], 30 * 3)
        self.assertEqual(s["final_total_pence"], 4000 + s["pnl_pence"])
        self.assertEqual(len(res["curve"]), 30)

    def test_small_stakes_shrink_but_protected_untouched(self) -> None:
        res = run_backtest(self.frame(200, a_wins=0.0), RiskConfig())
        s = res["summary"]
        self.assertEqual(s["final_protected_pence"], 0)
        self.assertLess(s["final_active_pence"], 4000)
        stakes = [b["stake_pence"] for b in res["bets"]]
        self.assertGreater(stakes[0], stakes[-1])  # stakes shrink as the bankroll shrinks; no chasing

    def test_losing_every_bet_exhausts_and_stops(self) -> None:
        res = run_backtest(self.frame(400, a_wins=0.0, p=0.9), RiskConfig())
        s = res["summary"]
        self.assertEqual(s["status"], "EXHAUSTED")
        self.assertIsNotNone(s["exhausted_on"])
        self.assertLess(s["final_active_pence"], RiskConfig().min_active_bankroll_pence)
        self.assertLess(len(res["curve"]), 400)  # stopped early


if __name__ == "__main__":
    unittest.main()
