"""Section 11 bankroll-protection tests.

Each test class maps to a requirement in Section 11E of the project spec.
Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import timedelta

from helpers import LOOSE, LedgerTestCase, auth_for_bankroll

from ufc_edge.errors import (
    AuthorisationError,
    LedgerIntegrityError,
    RiskRejected,
    SettlementError,
    StateError,
)
from ufc_edge.ledger import EXHAUSTED_BANNER, BankrollService, ManualAuthorisation


class TestSection11BTable(LedgerTestCase):
    """Replays the worked example table from Section 11B with real settlements."""

    config = LOOSE

    def test_spec_table(self) -> None:
        self.assert_balances(4000, 0)                      # Initial deposit: £40 / £0
        self.bet_and_settle(400, "WON", odds="3.50")       # +£10 -> funds reach £50
        self.assert_balances(4000, 1000)                   # £40 / £10
        self.bet_and_settle(300, "LOST")
        self.bet_and_settle(200, "LOST")                   # £5 net loss
        self.assert_balances(3500, 1000)                   # £35 / £10
        self.bet_and_settle(250, "WON", odds="5.00")       # £10 net profit
        self.assert_balances(4000, 1500)                   # £40 / £15
        for stake in (400, 350, 300, 250, 200):            # £15 net loss
            self.bet_and_settle(stake, "LOST")
        self.assert_balances(2500, 1500)                   # £25 / £15
        b = self.svc.balances()
        self.assertEqual(b.net_pnl_pence, 0)               # +10 -5 +10 -15
        self.assertEqual(b.total_funds_pence, 4000)


class TestProtectedProfitsNeverStakeable(LedgerTestCase):
    """11E: protected profits never become available betting funds."""

    config = LOOSE

    def setUp(self) -> None:
        super().setUp()
        self.bet_and_settle(400, "WON", odds="3.50")
        self.assert_balances(4000, 1000)
        self.new_event()

    def test_stake_needing_protected_funds_is_rejected(self) -> None:
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(4500))
        self.assertTrue(any(r.startswith("WOULD_USE_PROTECTED_FUNDS") for r in ctx.exception.reasons))
        self.assert_balances(4000, 1000)

    def test_stake_limits_ignore_protected_funds(self) -> None:
        # 10% of total funds (£50) would be £5, but limits use capital in play (£40) only.
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(401))
        self.assertIn("STAKE_EXCEEDS_PER_BET_LIMIT", ctx.exception.reasons)
        self.svc.place_bet(self.proposal(400))

    def test_loss_never_reduces_protected(self) -> None:
        """11E: a loss reduces the active bankroll without reducing protected profits."""
        self.bet_and_settle(400, "LOST")
        self.assert_balances(3600, 1000)

    def test_protected_transfer_requires_confirmation(self) -> None:
        with self.assertRaises(StateError):
            self.svc.record_protected_transfer(500, "BANK-123", transfer_confirmed=False)
        self.svc.record_protected_transfer(500, "BANK-123", transfer_confirmed=True)
        b = self.svc.balances()
        self.assertEqual((b.protected_pence, b.withdrawn_pence), (500, 500))
        with self.assertRaises(StateError):
            self.svc.record_protected_transfer(501, "BANK-124", transfer_confirmed=True)


class TestWinCannotExceedCap(LedgerTestCase):
    """11E: a win cannot increase the active bankroll above its permitted cap."""

    config = LOOSE

    def test_sweep_with_other_bets_still_open(self) -> None:
        a = self.svc.place_bet(self.proposal(400, "10.00"))
        self.svc.place_bet(self.proposal(400))
        self.settle(a.bet_id, "WON")  # returns £40
        b = self.svc.balances()
        self.assertEqual(b.capital_in_play_pence, 4000)   # exactly the cap
        self.assertEqual(b.open_stakes_pence, 400)
        self.assertEqual(b.protected_pence, 3600)          # £36 profit protected immediately

    def test_unsettled_winnings_are_not_counted(self) -> None:
        self.svc.place_bet(self.proposal(400, "10.00"))
        b = self.svc.balances()
        self.assertEqual((b.active_pence, b.open_stakes_pence, b.protected_pence, b.net_pnl_pence),
                         (3600, 400, 0, 0))


class TestExhaustion(LedgerTestCase):
    """11E: the system stops when exhausted, survives restarts and needs a manual restart."""

    def exhaust(self) -> None:
        cfg = self.svc.risk_config()
        for _ in range(500):
            if self.svc.status()["status"] == "EXHAUSTED":
                return
            self.clock.advance(hours=25)  # clear the rolling 24h loss window
            stake = int(cfg.max_stake_fraction * self.svc.balances().active_pence)
            self.bet_and_settle(stake, "LOST")
        self.fail("bankroll never exhausted")

    def test_stops_when_bankroll_exhausted(self) -> None:
        self.exhaust()
        status = self.svc.status()
        self.assertEqual(status["banner"], EXHAUSTED_BANNER)
        self.assertLess(status["active_pence"], self.svc.risk_config().min_active_bankroll_pence)
        self.new_event()
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(10))
        self.assertTrue(any(r.startswith("BANKROLL_EXHAUSTED") for r in ctx.exception.reasons))

    def test_restart_does_not_bypass_stop(self) -> None:
        self.exhaust()
        svc = self.reopen()
        self.assertEqual(svc.status()["status"], "EXHAUSTED")
        self.new_event()
        with self.assertRaises(RiskRejected):
            svc.place_bet(self.proposal(10))

    def test_database_blocks_clearing_latch_without_authorisation(self) -> None:
        self.exhaust()
        with self.assertRaises(sqlite3.DatabaseError):
            self.svc.conn.execute("UPDATE system_state SET status = 'ACTIVE' WHERE id = 1")
        self.assertEqual(self.svc.status()["status"], "EXHAUSTED")

    def test_manual_restart_required(self) -> None:
        self.exhaust()
        residual = self.svc.balances().active_pence
        bad = ManualAuthorisation("Anurag", "try again", "yes")
        with self.assertRaises(AuthorisationError):
            self.svc.authorise_new_bankroll(4000 - residual, bad)
        self.assertEqual(self.svc.status()["status"], "EXHAUSTED")
        self.svc.authorise_new_bankroll(4000 - residual, auth_for_bankroll(4000))
        self.assertEqual(self.svc.status()["status"], "ACTIVE")
        self.assertEqual(self.svc.balances().bankroll_cap_pence, 4000)
        self.new_event()
        self.svc.place_bet(self.proposal(100))

    def test_new_bankroll_only_after_exhaustion(self) -> None:
        with self.assertRaises(StateError):
            self.svc.authorise_new_bankroll(1000, auth_for_bankroll(5000))


class TestNoDuplicateWagers(LedgerTestCase):
    """11E: repeated API requests cannot duplicate wagers."""

    config = LOOSE

    def test_retry_with_same_key_is_idempotent(self) -> None:
        p = self.proposal(300, key="evt1-fight3-red")
        first = self.svc.place_bet(p)
        second = self.svc.place_bet(p)
        self.assertEqual(first.bet_id, second.bet_id)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assert_balances(3700, 0, open_stakes=300)

    def test_retry_after_restart_is_idempotent(self) -> None:
        p = self.proposal(300, key="evt1-fight3-red")
        self.svc.place_bet(p)
        self.reopen().place_bet(p)
        self.assert_balances(3700, 0, open_stakes=300)

    def test_same_selection_new_key_rejected(self) -> None:
        self.svc.place_bet(self.proposal(300, selection="Fighter A"))
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(300, selection="Fighter A"))
        self.assertTrue(any(r.startswith("DUPLICATE_SELECTION") for r in ctx.exception.reasons))

    def test_key_reused_for_different_terms_rejected(self) -> None:
        self.svc.place_bet(self.proposal(300, key="k1"))
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(200, key="k1"))
        self.assertTrue(ctx.exception.reasons[0].startswith("IDEMPOTENCY_KEY_REUSED"))


class TestConcurrentExposure(LedgerTestCase):
    """11E: concurrent bets cannot collectively exceed the exposure limit."""

    def test_parallel_requests_respect_event_limit(self) -> None:
        # Default config: event exposure 15% of £40 = £6, so at most three £2 bets.
        n = 8
        barrier = threading.Barrier(n)
        proposals = [self.proposal(200) for _ in range(n)]
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            svc = BankrollService(self.db_path, mode="paper", clock=self.clock)
            try:
                barrier.wait()
                svc.place_bet(proposals[i])
                result = "ok"
            except RiskRejected:
                result = "rejected"
            finally:
                svc.close()
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("ok"), 3)
        self.assertEqual(self.svc.balances().open_stakes_pence, 600)
        self.svc.verify_integrity()


class TestSettlementSafety(LedgerTestCase):
    """11E: incorrect or delayed settlements cannot release funds prematurely."""

    config = LOOSE

    def setUp(self) -> None:
        super().setUp()
        self.bet = self.svc.place_bet(self.proposal(400, "3.00"))

    def test_unverified_result_refused(self) -> None:
        self.clock.now += timedelta(hours=12)
        with self.assertRaises(SettlementError):
            self.svc.settle_bet(self.bet.bet_id, "WON", "twitter rumour", verified=False)
        self.assert_balances(3600, 0, open_stakes=400)

    def test_settlement_before_event_start_refused(self) -> None:
        with self.assertRaises(SettlementError):
            self.svc.settle_bet(self.bet.bet_id, "WON", "ufcstats.com", verified=True)
        self.assert_balances(3600, 0, open_stakes=400)

    def test_double_settlement_refused(self) -> None:
        self.settle(self.bet.bet_id, "LOST")
        with self.assertRaises(SettlementError):
            self.settle(self.bet.bet_id, "WON")
        self.assert_balances(3600, 0)

    def test_database_blocks_editing_settled_bet(self) -> None:
        self.settle(self.bet.bet_id, "LOST")
        with self.assertRaises(sqlite3.DatabaseError):
            self.svc.conn.execute("UPDATE bets SET status = 'WON' WHERE id = ?", (self.bet.bet_id,))

    def test_void_returns_stake_only(self) -> None:
        self.settle(self.bet.bet_id, "VOID")
        self.assert_balances(4000, 0)

    def test_ledger_is_append_only(self) -> None:
        with self.assertRaises(sqlite3.DatabaseError):
            self.svc.conn.execute("UPDATE ledger_lines SET amount_pence = 99999")
        with self.assertRaises(sqlite3.DatabaseError):
            self.svc.conn.execute("DELETE FROM ledger_transactions")


class TestStopControls(LedgerTestCase):
    """Kill switch, stale odds, fail-closed integrity and other risk rules."""

    def test_kill_switch(self) -> None:
        self.svc.engage_kill_switch("testing")
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(100))
        self.assertTrue(any(r.startswith("KILL_SWITCH") for r in ctx.exception.reasons))
        with self.assertRaises(AuthorisationError):
            self.svc.release_kill_switch(ManualAuthorisation("Anurag", "done", "ok"))
        self.svc.release_kill_switch(ManualAuthorisation("Anurag", "done", "RELEASE KILL SWITCH"))
        self.svc.place_bet(self.proposal(100))

    def test_stale_odds_rejected(self) -> None:
        old = self.clock() - timedelta(minutes=30)
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(100, captured_at=old))
        self.assertTrue(any(r.startswith("STALE_ODDS") for r in ctx.exception.reasons))

    def test_no_bets_after_event_start(self) -> None:
        p = self.proposal(100)
        self.clock.now += timedelta(hours=11)
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(p)
        self.assertTrue(any(r.startswith("EVENT_STARTED") for r in ctx.exception.reasons))

    def test_insufficient_edge_rejected(self) -> None:
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(100, p_model=0.38, p_market=0.38))
        self.assertTrue(any(r.startswith("INSUFFICIENT_EDGE") for r in ctx.exception.reasons))

    def test_max_bets_per_event(self) -> None:
        for _ in range(3):
            self.svc.place_bet(self.proposal(150))
        self.svc.place_bet(self.proposal(100))
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(10))
        self.assertIn("MAX_BETS_PER_EVENT", ctx.exception.reasons)

    def test_daily_loss_limit(self) -> None:
        # Default: worst-case 24h loss capped at 20% of £40 = £8.
        # Four £2 bets on four separate events, all lost within the same 24 hours.
        bets = []
        for _ in range(4):
            self.new_event()
            bets.append(self.svc.place_bet(self.proposal(200)))
        for bet in bets:
            self.settle(bet.bet_id, "LOST")
        self.new_event()
        with self.assertRaises(RiskRejected) as ctx:
            self.svc.place_bet(self.proposal(10))  # £8.00 lost + £0.10 > £8.00 limit
        self.assertTrue(any(r.startswith("DAILY_LOSS_LIMIT") for r in ctx.exception.reasons))
        self.clock.advance(hours=25)
        self.new_event()
        self.svc.place_bet(self.proposal(10))

    def test_integrity_failure_fails_closed(self) -> None:
        self.svc.place_bet(self.proposal(100))
        # Simulate corruption by bypassing the append-only trigger.
        self.svc.conn.execute("DROP TRIGGER trg_ledger_lines_no_update")
        self.svc.conn.execute(
            "UPDATE ledger_lines SET amount_pence = amount_pence + 1 WHERE account = 'ACTIVE'"
            " AND id = (SELECT MAX(id) FROM ledger_lines WHERE account = 'ACTIVE')"
        )
        with self.assertRaises(LedgerIntegrityError):
            self.svc.verify_integrity()
        self.assertTrue(self.svc.status()["halted"])
        with self.assertRaises(LedgerIntegrityError):
            self.svc.place_bet(self.proposal(100))

    def test_risk_change_needs_authorisation(self) -> None:
        from dataclasses import replace

        cfg = replace(self.svc.risk_config(), max_stake_fraction=0.08)
        with self.assertRaises(AuthorisationError):
            self.svc.update_risk_config(cfg, ManualAuthorisation("Anurag", "why", "yes"))
        self.assertEqual(self.svc.risk_config().max_stake_fraction, 0.05)
        with self.assertRaises(ValueError):
            self.svc.update_risk_config(
                replace(cfg, max_stake_fraction=0.5),
                ManualAuthorisation("Anurag", "why", "AUTHORISE RISK CHANGE"),
            )


if __name__ == "__main__":
    unittest.main()
