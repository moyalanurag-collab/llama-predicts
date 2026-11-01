"""Bankroll ledger and the only code path that can record or settle a bet.

Accounting model (double-entry, integer pence)
-----------------------------------------------
Every money movement is a ledger transaction whose lines sum to zero.

    OWNER        your money coming in (goes negative as you deposit)
    ACTIVE       betting bankroll available to stake
    OPEN_STAKES  stakes on bets that have not settled yet
    PROTECTED    realised profit, never stakeable
    WITHDRAWN    protected profit confirmed as moved out of the betting account
    MARKET       the bookmaker side; -MARKET is your total betting profit/loss

    Deposit £40          OWNER -4000   ACTIVE +4000
    Place £2 bet         ACTIVE -200   OPEN_STAKES +200
    Bet wins at 3.00     OPEN_STAKES -200  ACTIVE +600  MARKET -400
    Bet loses            OPEN_STAKES -200  MARKET +200
    Bet void             OPEN_STAKES -200  ACTIVE +200
    Profit sweep         ACTIVE -x     PROTECTED +x
    Confirmed transfer   PROTECTED -x  WITHDRAWN +x

The capital boundary: after every settlement, if ACTIVE + OPEN_STAKES exceeds
the authorised bankroll cap (£40), the excess is swept from ACTIVE into
PROTECTED straight away. Unsettled potential winnings never appear anywhere.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from . import db
from .config import RiskConfig
from .errors import (
    AuthorisationError,
    LedgerIntegrityError,
    RiskRejected,
    SettlementError,
    StateError,
)
from .money import fmt, winning_return_pence
from .risk import BetProposal, RiskSnapshot, evaluate
from .timeutil import from_iso, to_iso, utcnow

EXHAUSTED_BANNER = "BANKROLL EXHAUSTED — BETTING DISABLED"


def bankroll_phrase(pence: int) -> str:
    """Confirmation phrase for authorising a bankroll, e.g. 'AUTHORISE BANKROLL 40.00'."""
    return f"AUTHORISE BANKROLL {pence // 100}.{pence % 100:02d}"


class _IntegrityAbort(Exception):
    """Internal signal: roll back the current transaction, then fail closed."""


def _fails_closed(method: Callable) -> Callable:
    """Turn an in-transaction integrity failure into a halt plus LedgerIntegrityError."""

    @functools.wraps(method)
    def wrapper(self: "BankrollService", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except _IntegrityAbort as exc:
            self._fail_closed(self._integrity_problems() or [str(exc) or "integrity check failed"])

    return wrapper


@dataclass(frozen=True)
class ManualAuthorisation:
    """A human's explicit sign-off for a protected action.

    ``confirmation`` must match the exact phrase the action asks for (for
    example ``"AUTHORISE BANKROLL 40.00"``). That makes every authorisation
    deliberate and specific to one amount or action. These methods are only
    exposed through the operator CLI, never to the prediction/agent code.
    """

    operator: str
    reason: str
    confirmation: str


@dataclass(frozen=True)
class Balances:
    active_pence: int
    open_stakes_pence: int
    protected_pence: int
    withdrawn_pence: int
    owner_contributed_pence: int
    net_pnl_pence: int
    bankroll_cap_pence: int

    @property
    def capital_in_play_pence(self) -> int:
        return self.active_pence + self.open_stakes_pence

    @property
    def total_funds_pence(self) -> int:
        """Everything you own here: in play + protected + withdrawn."""
        return self.active_pence + self.open_stakes_pence + self.protected_pence + self.withdrawn_pence


@dataclass(frozen=True)
class BetResult:
    bet_id: int
    created: bool  # False when an identical retry returned the existing bet
    overridden: tuple[str, ...] = ()  # limit checks skipped (paper experiment mode only)


# Limit checks a PAPER experiment may skip. Hard stops (kill switch, exhaustion, halt, event
# started, stake above the active bankroll, protected funds, duplicates) can never be skipped.
EXPERIMENT_OVERRIDABLE = {
    "STAKE_EXCEEDS_PER_BET_LIMIT", "EVENT_EXPOSURE_LIMIT", "TOTAL_EXPOSURE_LIMIT", "DAILY_LOSS_LIMIT",
    "EVENT_LOSS_LIMIT", "MAX_BETS_PER_EVENT", "INSUFFICIENT_EDGE", "STALE_ODDS", "BELOW_MIN_BANKROLL",
}


class BankrollService:
    def __init__(
        self,
        db_path: str | Path,
        mode: str = "paper",
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.mode = mode
        self.clock = clock
        self.conn = db.connect(db_path)
        db.init_schema(self.conn, mode, to_iso(clock()))

    def close(self) -> None:
        self.conn.close()

    def scratch_copy(self, path: str | Path) -> "BankrollService":
        """An exact copy of this ledger in another file, for dry runs.

        The betting plan is computed by placing the proposed bets on the copy,
        so the real risk engine sees each bet's effect on later ones
        (exposure, bet counts). The real ledger is never touched.
        """
        dest = sqlite3.connect(str(path))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        return BankrollService(path, mode=self.mode, clock=self.clock)

    # ------------------------------------------------------------------ infra
    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run a block under SQLite's write lock; commit on success, roll back on error."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _audit(self, actor: str, action: str, payload: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(created_at, actor, action, payload) VALUES (?,?,?,?)",
            (to_iso(self.clock()), actor, action, json.dumps(payload or {}, default=str, sort_keys=True)),
        )

    def _post(
        self,
        kind: str,
        lines: dict[str, int],
        memo: str = "",
        bet_id: int | None = None,
        authorisation_id: int | None = None,
    ) -> int:
        """Write one balanced ledger transaction. Zero-amount lines are dropped."""
        lines = {acct: amt for acct, amt in lines.items() if amt != 0}
        if sum(lines.values()) != 0:
            raise LedgerIntegrityError(f"Unbalanced {kind} transaction: {lines}")
        if len(lines) < 2:
            raise LedgerIntegrityError(f"{kind} transaction needs at least two lines")
        cur = self.conn.execute(
            "INSERT INTO ledger_transactions(created_at, kind, bet_id, authorisation_id, memo)"
            " VALUES (?,?,?,?,?)",
            (to_iso(self.clock()), kind, bet_id, authorisation_id, memo),
        )
        txn_id = int(cur.lastrowid)
        self.conn.executemany(
            "INSERT INTO ledger_lines(txn_id, account, amount_pence) VALUES (?,?,?)",
            [(txn_id, acct, amt) for acct, amt in lines.items()],
        )
        return txn_id

    def _state(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM system_state WHERE id = 1").fetchone()

    def _set_state(self, **fields: object) -> None:
        fields["updated_at"] = to_iso(self.clock())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE system_state SET {cols} WHERE id = 1", tuple(fields.values()))

    def _record_authorisation(self, kind: str, auth: ManualAuthorisation, expected: str, payload: dict) -> int:
        if not auth.operator.strip() or not auth.reason.strip():
            raise AuthorisationError("Operator name and reason are required.")
        if auth.confirmation != expected:
            raise AuthorisationError(f"Confirmation must be exactly: {expected!r}")
        cur = self.conn.execute(
            "INSERT INTO authorisations(created_at, kind, operator, reason, payload) VALUES (?,?,?,?,?)",
            (to_iso(self.clock()), kind, auth.operator, auth.reason, json.dumps(payload, sort_keys=True)),
        )
        return int(cur.lastrowid)

    # --------------------------------------------------------------- queries
    def balances(self) -> Balances:
        sums = {
            r["account"]: r["total"]
            for r in self.conn.execute(
                "SELECT account, SUM(amount_pence) AS total FROM ledger_lines GROUP BY account"
            )
        }
        return Balances(
            active_pence=sums.get("ACTIVE", 0),
            open_stakes_pence=sums.get("OPEN_STAKES", 0),
            protected_pence=sums.get("PROTECTED", 0),
            withdrawn_pence=sums.get("WITHDRAWN", 0),
            owner_contributed_pence=-sums.get("OWNER", 0),
            net_pnl_pence=-sums.get("MARKET", 0),
            bankroll_cap_pence=self._state()["bankroll_cap_pence"],
        )

    def risk_config(self) -> RiskConfig:
        row = self.conn.execute(
            "SELECT config_json FROM risk_config_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return RiskConfig.from_json(row["config_json"]) if row else RiskConfig()

    def status(self) -> dict:
        st = self._state()
        b = self.balances()
        if st["status"] == "EXHAUSTED":
            banner = EXHAUSTED_BANNER
        elif st["halted"]:
            banner = f"HALTED — {st['halt_reason']}"
        elif st["kill_switch"]:
            banner = f"KILL SWITCH ENGAGED — {st['kill_reason']}"
        elif st["status"] == "UNINITIALISED":
            banner = "NOT INITIALISED — no bankroll authorised"
        else:
            banner = "ACTIVE"
        return {
            "mode": st["mode"],
            "status": st["status"],
            "banner": banner,
            "kill_switch": bool(st["kill_switch"]),
            "halted": bool(st["halted"]),
            "protected_funds_note": (
                "Protected profits are ledger-protected (accounting-protected). They are only "
                "physically segregated once a transfer out has been confirmed (WITHDRAWN)."
            ),
            **asdict(b),
        }

    # ------------------------------------------------------------- integrity
    def _integrity_problems(self) -> list[str]:
        c = self.conn
        problems: list[str] = []
        for r in c.execute(
            "SELECT txn_id, SUM(amount_pence) s FROM ledger_lines GROUP BY txn_id HAVING s <> 0"
        ):
            problems.append(f"transaction {r['txn_id']} does not balance ({r['s']})")
        orphan = c.execute(
            "SELECT COUNT(*) FROM ledger_transactions t WHERE "
            "(SELECT COUNT(*) FROM ledger_lines l WHERE l.txn_id = t.id) < 2"
        ).fetchone()[0]
        if orphan:
            problems.append(f"{orphan} transaction(s) with fewer than two lines")
        b = self.balances()
        for name in ("active_pence", "open_stakes_pence", "protected_pence", "withdrawn_pence"):
            if getattr(b, name) < 0:
                problems.append(f"{name} is negative ({getattr(b, name)})")
        open_sum = c.execute(
            "SELECT COALESCE(SUM(stake_pence), 0) FROM bets WHERE status = 'OPEN'"
        ).fetchone()[0]
        if open_sum != b.open_stakes_pence:
            problems.append(f"open stakes ledger {b.open_stakes_pence} != open bets {open_sum}")
        st = self._state()
        if st["status"] == "ACTIVE" and b.capital_in_play_pence > b.bankroll_cap_pence:
            problems.append("capital in play exceeds the authorised bankroll cap")
        return problems

    def _fail_closed(self, problems: list[str]) -> None:
        """Halt in its own transaction, then raise."""
        reason = "; ".join(problems)
        with self._write():
            self._set_state(halted=1, halt_reason=reason)
            self._audit("system", "HALTED_INTEGRITY", {"problems": problems})
        raise LedgerIntegrityError(reason)

    def verify_integrity(self) -> None:
        """Reconcile the ledger. On any mismatch the system halts (fails closed)."""
        problems = self._integrity_problems()
        if problems:
            self._fail_closed(problems)

    # ---------------------------------------------------------- bankroll setup
    def initialise_bankroll(self, amount_pence: int, auth: ManualAuthorisation) -> None:
        expected = bankroll_phrase(amount_pence)
        with self._write():
            if self._state()["status"] != "UNINITIALISED":
                raise StateError("Bankroll already initialised.")
            cfg = self.risk_config()
            if amount_pence < cfg.min_active_bankroll_pence:
                raise StateError("Bankroll is below the minimum active bankroll threshold.")
            auth_id = self._record_authorisation(
                "INITIAL_BANKROLL", auth, expected, {"amount_pence": amount_pence}
            )
            self.conn.execute(
                "INSERT INTO risk_config_history(created_at, authorisation_id, config_json) VALUES (?,?,?)",
                (to_iso(self.clock()), auth_id, cfg.to_json()),
            )
            self._post("DEPOSIT", {"OWNER": -amount_pence, "ACTIVE": amount_pence},
                       "Initial bankroll", authorisation_id=auth_id)
            self._set_state(status="ACTIVE", bankroll_cap_pence=amount_pence,
                            last_authorisation_id=auth_id)
            self._audit("operator", "BANKROLL_INITIALISED",
                        {"amount_pence": amount_pence, "operator": auth.operator})

    def authorise_new_bankroll(self, deposit_pence: int, auth: ManualAuthorisation) -> None:
        """The only way out of BANKROLL EXHAUSTED. Never called automatically."""
        if deposit_pence < 0:
            raise ValueError("Deposit cannot be negative.")
        with self._write():
            st = self._state()
            if st["status"] != "EXHAUSTED":
                raise StateError("A new bankroll can only be authorised after exhaustion.")
            b = self.balances()
            if b.open_stakes_pence:
                raise StateError("Open bets must settle before a new bankroll is authorised.")
            new_cap = b.active_pence + deposit_pence
            if new_cap < self.risk_config().min_active_bankroll_pence:
                raise StateError("New bankroll would still be below the minimum threshold.")
            expected = bankroll_phrase(new_cap)
            auth_id = self._record_authorisation(
                "NEW_BANKROLL", auth, expected,
                {"deposit_pence": deposit_pence, "new_cap_pence": new_cap},
            )
            if deposit_pence:
                self._post("DEPOSIT", {"OWNER": -deposit_pence, "ACTIVE": deposit_pence},
                           "New bankroll after exhaustion", authorisation_id=auth_id)
            self._set_state(status="ACTIVE", bankroll_cap_pence=new_cap, last_authorisation_id=auth_id)
            self._audit("operator", "BANKROLL_RESTARTED",
                        {"deposit_pence": deposit_pence, "new_cap_pence": new_cap,
                         "operator": auth.operator})

    def update_risk_config(self, cfg: RiskConfig, auth: ManualAuthorisation) -> None:
        cfg.validate()
        with self._write():
            auth_id = self._record_authorisation(
                "RISK_CHANGE", auth, "AUTHORISE RISK CHANGE", json.loads(cfg.to_json())
            )
            self.conn.execute(
                "INSERT INTO risk_config_history(created_at, authorisation_id, config_json) VALUES (?,?,?)",
                (to_iso(self.clock()), auth_id, cfg.to_json()),
            )
            self._audit("operator", "RISK_CONFIG_CHANGED", json.loads(cfg.to_json()))

    # ------------------------------------------------------------ stop controls
    def engage_kill_switch(self, reason: str, actor: str = "operator") -> None:
        """Stopping never needs authorisation; restarting does."""
        with self._write():
            self._set_state(kill_switch=1, kill_reason=reason or "no reason given")
            self._audit(actor, "KILL_SWITCH_ENGAGED", {"reason": reason})

    def release_kill_switch(self, auth: ManualAuthorisation) -> None:
        with self._write():
            auth_id = self._record_authorisation("KILL_SWITCH_RELEASE", auth, "RELEASE KILL SWITCH", {})
            self._set_state(kill_switch=0, kill_reason=None, last_authorisation_id=auth_id)
            self._audit("operator", "KILL_SWITCH_RELEASED", {"operator": auth.operator})

    def halt(self, reason: str, actor: str = "system") -> None:
        with self._write():
            self._set_state(halted=1, halt_reason=reason)
            self._audit(actor, "HALTED", {"reason": reason})

    def clear_halt(self, auth: ManualAuthorisation) -> None:
        problems = self._integrity_problems()
        if problems:
            raise StateError("Cannot clear halt; integrity problems remain: " + "; ".join(problems))
        with self._write():
            auth_id = self._record_authorisation("HALT_CLEAR", auth, "CLEAR HALT", {})
            self._set_state(halted=0, halt_reason=None, last_authorisation_id=auth_id)
            self._audit("operator", "HALT_CLEARED", {"operator": auth.operator})

    # ------------------------------------------------------------------ events
    def upsert_event(self, event_id: str, name: str, starts_at: datetime) -> None:
        with self._write():
            self.conn.execute(
                "INSERT INTO events(id, name, starts_at) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, starts_at = excluded.starts_at",
                (event_id, name, to_iso(starts_at)),
            )

    # -------------------------------------------------------------------- bets
    def _snapshot(self, p: BetProposal, now: datetime) -> RiskSnapshot:
        c = self.conn
        st = self._state()
        b = self.balances()
        ev_row = c.execute("SELECT starts_at FROM events WHERE id = ?", (p.event_id,)).fetchone()
        one = lambda sql, *args: c.execute(sql, args).fetchone()[0]  # noqa: E731
        return RiskSnapshot(
            status=st["status"],
            kill_switch=bool(st["kill_switch"]),
            halted=bool(st["halted"]),
            bankroll_cap_pence=st["bankroll_cap_pence"],
            active_pence=b.active_pence,
            open_stakes_pence=b.open_stakes_pence,
            protected_pence=b.protected_pence,
            event_starts_at=from_iso(ev_row["starts_at"]) if ev_row else None,
            event_open_stakes_pence=one(
                "SELECT COALESCE(SUM(stake_pence),0) FROM bets WHERE event_id=? AND status='OPEN'",
                p.event_id),
            event_bet_count=one(
                "SELECT COUNT(*) FROM bets WHERE event_id=? AND status<>'VOID'", p.event_id),
            event_realised_pnl_pence=one(
                "SELECT COALESCE(SUM(returned_pence - stake_pence),0) FROM bets "
                "WHERE event_id=? AND status IN ('WON','LOST')", p.event_id),
            realised_pnl_24h_pence=one(
                "SELECT COALESCE(SUM(returned_pence - stake_pence),0) FROM bets "
                "WHERE status IN ('WON','LOST') AND settled_at >= ?",
                to_iso(now - timedelta(hours=24))),
            selection_already_bet=bool(one(
                "SELECT COUNT(*) FROM bets WHERE event_id=? AND fight_id=? AND market=? "
                "AND selection=? AND status<>'VOID'",
                p.event_id, p.fight_id, p.market, p.selection)),
        )

    @_fails_closed
    def place_bet(self, p: BetProposal, actor: str = "agent", beyond_limits: bool = False) -> BetResult:
        """Validate a proposal with the risk engine and, if approved, record it.

        Checking and recording happen in one ``BEGIN IMMEDIATE`` transaction,
        so concurrent requests are serialised and cannot jointly breach a limit.
        Retrying with the same idempotency key returns the original bet.
        """
        now = self.clock()
        rejection: list[str] | None = None
        if beyond_limits and self.mode != "paper":
            raise StateError("Beyond-limits experiments are only allowed on the paper ledger.")
        with self._write():
            existing = self.conn.execute(
                "SELECT * FROM bets WHERE idempotency_key = ?", (p.idempotency_key,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["event_id"] == p.event_id and existing["fight_id"] == p.fight_id
                    and existing["market"] == p.market and existing["selection"] == p.selection
                    and existing["stake_pence"] == p.stake_pence
                    and existing["decimal_odds"] == str(p.decimal_odds)
                )
                self._audit(actor, "BET_RETRY_DEDUPLICATED" if same else "BET_KEY_CONFLICT",
                            {"idempotency_key": p.idempotency_key, "bet_id": existing["id"]})
                if same:
                    return BetResult(existing["id"], created=False)
                rejection = ["IDEMPOTENCY_KEY_REUSED: key already used for a different bet"]
            else:
                problems = self._integrity_problems()
                if problems:
                    raise _IntegrityAbort("; ".join(problems))
                snap = self._snapshot(p, now)
                reasons = evaluate(p, snap, self.risk_config(), now)
                overridden: list[str] = []
                if beyond_limits:
                    overridden = [r for r in reasons if r.split(":")[0] in EXPERIMENT_OVERRIDABLE]
                    reasons = [r for r in reasons if r not in overridden]
                if reasons:
                    rejection = reasons
                    self._audit(actor, "BET_REJECTED",
                                {"proposal": asdict(p), "reasons": reasons, "snapshot": asdict(snap)})
                else:
                    cur = self.conn.execute(
                        """INSERT INTO bets(idempotency_key, event_id, fight_id, market, selection,
                               decimal_odds, stake_pence, odds_source, odds_captured_at,
                               model_probability, placed_at, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'OPEN')""",
                        (p.idempotency_key, p.event_id, p.fight_id, p.market, p.selection,
                         str(p.decimal_odds), p.stake_pence, p.odds_source,
                         to_iso(p.odds_captured_at), p.model_probability, to_iso(now)),
                    )
                    bet_id = int(cur.lastrowid)
                    self._post("BET_PLACED", {"ACTIVE": -p.stake_pence, "OPEN_STAKES": p.stake_pence},
                               f"{p.selection} @ {p.decimal_odds}", bet_id=bet_id)
                    self._audit(actor, "BET_RECORDED_BEYOND_LIMITS" if overridden else "BET_RECORDED",
                                {"bet_id": bet_id, "proposal": asdict(p), "snapshot": asdict(snap),
                                 "overridden": overridden})
                    return BetResult(bet_id, created=True, overridden=tuple(overridden))
        if rejection is not None:
            raise RiskRejected(rejection)
        raise AssertionError("unreachable")  # pragma: no cover

    @_fails_closed
    def settle_bet(
        self,
        bet_id: int,
        outcome: str,
        result_source: str,
        verified: bool,
        actor: str = "operator",
    ) -> None:
        """Settle an open bet against a *verified* result.

        ``outcome`` is WON, LOST or VOID (VOID covers draws/no contests in
        draw-no-bet moneyline markets, cancelled fights and voided bets).
        """
        if outcome not in ("WON", "LOST", "VOID"):
            raise SettlementError(f"Unknown outcome {outcome!r}")
        if not verified:
            with self._write():
                self._audit(actor, "SETTLEMENT_REFUSED_UNVERIFIED",
                            {"bet_id": bet_id, "outcome": outcome, "source": result_source})
            raise SettlementError("Settlement refused: result is not verified.")
        if not result_source.strip():
            raise SettlementError("A result source is required.")
        now = self.clock()
        with self._write():
            bet = self.conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
            if bet is None:
                raise SettlementError(f"No bet {bet_id}")
            if bet["status"] != "OPEN":
                raise SettlementError(f"Bet {bet_id} is already settled ({bet['status']}).")
            starts = self.conn.execute(
                "SELECT starts_at FROM events WHERE id = ?", (bet["event_id"],)
            ).fetchone()["starts_at"]
            if outcome != "VOID" and now < from_iso(starts):
                raise SettlementError("Premature settlement: the event has not started.")

            stake = bet["stake_pence"]
            if outcome == "WON":
                returned = winning_return_pence(stake, Decimal(bet["decimal_odds"]))
                lines = {"OPEN_STAKES": -stake, "ACTIVE": returned, "MARKET": -(returned - stake)}
                kind = "BET_WON"
            elif outcome == "LOST":
                returned = 0
                lines = {"OPEN_STAKES": -stake, "MARKET": stake}
                kind = "BET_LOST"
            else:
                returned = stake
                lines = {"OPEN_STAKES": -stake, "ACTIVE": stake}
                kind = "BET_VOID"

            self.conn.execute(
                "UPDATE bets SET status=?, settled_at=?, result_source=?, returned_pence=? WHERE id=?",
                (outcome, to_iso(now), result_source, returned, bet_id),
            )
            self._post(kind, lines, f"Settle bet {bet_id}: {outcome}", bet_id=bet_id)
            self._audit(actor, "BET_SETTLED",
                        {"bet_id": bet_id, "outcome": outcome, "returned_pence": returned,
                         "source": result_source})
            self._sweep_profits()
            self._check_exhaustion()
            problems = self._integrity_problems()
            if problems:
                raise _IntegrityAbort("; ".join(problems))

    def _sweep_profits(self) -> None:
        b = self.balances()
        excess = b.capital_in_play_pence - b.bankroll_cap_pence
        if excess > 0:
            self._post("PROFIT_SWEEP", {"ACTIVE": -excess, "PROTECTED": excess},
                       "Realised profit above bankroll cap moved to protected")
            self._audit("system", "PROFIT_PROTECTED", {"amount_pence": excess})

    def _check_exhaustion(self) -> None:
        st = self._state()
        if st["status"] != "ACTIVE":
            return
        b = self.balances()
        cfg = self.risk_config()
        max_possible_stake = int(Decimal(str(cfg.max_stake_fraction)) * b.active_pence)
        cannot_bet = (
            b.active_pence < cfg.min_active_bankroll_pence
            or max_possible_stake < cfg.min_stake_pence
        )
        if b.open_stakes_pence == 0 and cannot_bet:
            self._set_state(status="EXHAUSTED")
            self._audit("system", "BANKROLL_EXHAUSTED",
                        {"residual_active_pence": b.active_pence,
                         "protected_pence": b.protected_pence})

    def record_protected_transfer(self, amount_pence: int, reference: str, transfer_confirmed: bool) -> None:
        """Record protected profit as physically moved out, only once the transfer is confirmed."""
        if not transfer_confirmed:
            raise StateError("Transfer not confirmed; funds remain accounting-protected only.")
        if amount_pence <= 0 or not reference.strip():
            raise ValueError("Positive amount and a transfer reference are required.")
        with self._write():
            if amount_pence > self.balances().protected_pence:
                raise StateError("Cannot transfer more than the protected balance.")
            self._post("PROTECTED_TRANSFER_OUT", {"PROTECTED": -amount_pence, "WITHDRAWN": amount_pence},
                       f"Transfer ref {reference}")
            self._audit("operator", "PROTECTED_TRANSFERRED",
                        {"amount_pence": amount_pence, "reference": reference})

    # ----------------------------------------------------------------- reads
    def ledger_entries(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT t.id, t.created_at, t.kind, t.bet_id, t.memo, l.account, l.amount_pence
               FROM ledger_transactions t JOIN ledger_lines l ON l.txn_id = t.id
               ORDER BY t.id, l.id"""
        ).fetchall()
        return [dict(r) for r in rows]
