"""Shared test fixtures: a controllable clock and a ready-to-use paper bankroll."""

from __future__ import annotations

import itertools
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from ufc_edge.config import RiskConfig
from ufc_edge.ledger import BankrollService, ManualAuthorisation, bankroll_phrase
from ufc_edge.risk import BetProposal
from ufc_edge.timeutil import from_iso

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
EVENT_START = T0 + timedelta(hours=10)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def auth_for_bankroll(pence: int) -> ManualAuthorisation:
    return ManualAuthorisation("Operator", "test bankroll", bankroll_phrase(pence))


# Permissive-but-realistic limits so individual tests can isolate one rule.
LOOSE = RiskConfig(
    max_stake_fraction=0.10,
    max_event_exposure_fraction=0.5,
    max_total_exposure_fraction=0.5,
    max_daily_loss_fraction=1.0,
    max_event_loss_fraction=1.0,
    max_bets_per_event=50,
    min_edge=0.0,
    min_stake_pence=1,
    min_active_bankroll_pence=1,
    max_decimal_odds="20.00",
    min_decimal_odds="1.01",
)

_counter = itertools.count(1)


class LedgerTestCase(unittest.TestCase):
    bankroll_pence = 4000
    config: RiskConfig | None = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "paper.db"
        self.clock = Clock()
        self.svc = BankrollService(self.db_path, mode="paper", clock=self.clock)
        self.svc.initialise_bankroll(self.bankroll_pence, auth_for_bankroll(self.bankroll_pence))
        if self.config is not None:
            self.svc.update_risk_config(
                self.config, ManualAuthorisation("Operator", "test config", "AUTHORISE RISK CHANGE")
            )
        self.svc.upsert_event("ufc-test", "UFC Test Night", EVENT_START)
        self.event_id = "ufc-test"

    def new_event(self) -> str:
        """Create an event starting 10 hours from the current (test) time."""
        event_id = f"ufc-{next(_counter)}"
        self.svc.upsert_event(event_id, "UFC Fight Night", self.clock() + timedelta(hours=10))
        self.event_id = event_id
        return event_id

    def tearDown(self) -> None:
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reopen(self) -> BankrollService:
        """Simulate an application restart."""
        self.svc.close()
        self.svc = BankrollService(self.db_path, mode="paper", clock=self.clock)
        return self.svc

    def proposal(self, stake: int, odds: str = "2.50", *, selection: str | None = None,
                 key: str | None = None, event_id: str | None = None, p_model: float = 0.55,
                 p_market: float = 0.38, captured_at: datetime | None = None) -> BetProposal:
        n = next(_counter)
        return BetProposal(
            idempotency_key=key or f"key-{n}",
            event_id=event_id or self.event_id,
            fight_id=f"fight-{selection or n}",
            market="moneyline",
            selection=selection or f"Fighter {n}",
            decimal_odds=Decimal(odds),
            stake_pence=stake,
            odds_source="test-feed",
            odds_captured_at=captured_at or self.clock(),
            model_probability=p_model,
            market_probability=p_market,
        )

    def settle(self, bet_id: int, outcome: str) -> None:
        """Move the clock past the bet's event start, then settle with a verified result."""
        starts = self.svc.conn.execute(
            "SELECT e.starts_at FROM bets b JOIN events e ON e.id = b.event_id WHERE b.id = ?",
            (bet_id,),
        ).fetchone()[0]
        after = from_iso(starts) + timedelta(hours=1)
        if self.clock() < after:
            self.clock.now = after
        self.svc.settle_bet(bet_id, outcome, "ufcstats.com (verified)", verified=True)

    def bet_and_settle(self, stake: int, outcome: str, odds: str = "2.50") -> None:
        """Place one bet on a fresh event and settle it (a full betting cycle)."""
        self.new_event()
        bet = self.svc.place_bet(self.proposal(stake, odds))
        self.settle(bet.bet_id, outcome)

    def assert_balances(self, active: int, protected: int, open_stakes: int = 0) -> None:
        b = self.svc.balances()
        self.assertEqual(
            (b.active_pence, b.protected_pence, b.open_stakes_pence),
            (active, protected, open_stakes),
        )
