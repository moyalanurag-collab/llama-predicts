"""Turn predictions into a betting plan.

The strategy only *proposes*. Every proposal then goes through the
independent risk engine (``BankrollService.place_bet``), which recomputes the
edge, re-checks the stake against hard limits, and can reject anything.

The plan for the dashboard is a dry run: the proposals are placed on a
scratch copy of the ledger, so the real risk engine decides each one and
sees how earlier bets affect later ones. Nothing is recorded.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

from .config import RiskConfig
from .errors import RiskRejected, UfcEdgeError
from .ev import recommend_stake
from .ledger import BankrollService
from .risk import BetProposal


@dataclass
class PlannedBet:
    proposal: BetProposal
    event_name: str
    event_starts_at: datetime
    commence_time: str
    api_event_id: str
    fighter_id: str
    fighter_name: str
    opponent_id: str
    opponent_name: str
    bookmaker: str
    p_model: float          # the model's (market-blended) probability for this fighter
    p_market: float         # consensus de-vigged market probability
    p_used: float           # after the risk config's extra shrink towards the market
    ev: float               # per £1 at the quoted odds, using p_used
    status: str = "proposed"   # proposed | approved | rejected | recorded
    reasons: list[str] = field(default_factory=list)
    bet_id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        p = d.pop("proposal")
        d.update(stake_pence=p["stake_pence"], decimal_odds=str(p["decimal_odds"]),
                 idempotency_key=p["idempotency_key"], event_id=p["event_id"],
                 odds_captured_at=p["odds_captured_at"].isoformat())
        d["event_starts_at"] = self.event_starts_at.isoformat()
        d["potential_return_pence"] = int(Decimal(p["stake_pence"]) * Decimal(str(p["decimal_odds"])))
        return d


def _cards(preds: pd.DataFrame, gap_hours: float = 12) -> pd.Series:
    """Card label per fight: fights within ``gap_hours`` of each other share a card."""
    times = pd.to_datetime(preds["commence_time"], utc=True)
    order = times.sort_values()
    label, prev, card = {}, None, 0
    for idx, t in order.items():
        if prev is not None and (t - prev).total_seconds() > gap_hours * 3600:
            card += 1
        label[idx] = card
        prev = t
    return pd.Series(label)


def build_plan(preds: pd.DataFrame, svc: BankrollService, cfg: RiskConfig | None = None) -> list[PlannedBet]:
    """Proposals for every side whose edge clears the minimum, best edge first."""
    if preds.empty:
        return []
    cfg = cfg or svc.risk_config()
    capital = svc.balances().capital_in_play_pence
    preds = preds.copy()
    preds["_card"] = _cards(preds)
    starts = preds.groupby("_card")["commence_time"].min()
    planned: list[PlannedBet] = []
    for _, r in preds.iterrows():
        card_start = pd.Timestamp(starts[r["_card"]]).to_pydatetime()
        event_id = f"card:{card_start:%Y-%m-%d}"
        captured = pd.Timestamp(r["odds_captured_at"]).to_pydatetime()
        for side, other in ((1, 2), (2, 1)):
            p_model = r["p_model_1"] if side == 1 else 1 - r["p_model_1"]
            p_mkt = r["p_market_1"] if side == 1 else 1 - r["p_market_1"]
            odds = float(r[f"best_odds_{side}"])
            rec = recommend_stake(
                p_model=p_model, p_market=p_mkt, decimal_odds=odds, capital_in_play_pence=capital,
                kelly_multiplier=cfg.kelly_fraction, model_weight=cfg.model_weight, min_edge=cfg.min_edge,
                max_stake_fraction=cfg.max_stake_fraction, min_stake_pence=cfg.min_stake_pence,
            )
            if not rec.qualifies:
                continue
            fighter_id = r[f"fighter_{side}_id"]
            planned.append(PlannedBet(
                proposal=BetProposal(
                    idempotency_key=f"{r['api_event_id']}:{fighter_id}",
                    event_id=event_id,
                    fight_id=str(r["api_event_id"]),
                    market="moneyline",
                    selection=r[f"fighter_{side}"],
                    decimal_odds=Decimal(f"{odds:.2f}"),
                    stake_pence=rec.stake_pence,
                    odds_source=f"The Odds API / {r[f'best_book_{side}']}",
                    odds_captured_at=captured,
                    model_probability=float(min(max(p_model, 1e-4), 1 - 1e-4)),
                    market_probability=float(min(max(p_mkt, 1e-4), 1 - 1e-4)),
                ),
                event_name=f"UFC card {card_start:%a %d %b %Y}",
                event_starts_at=card_start,
                commence_time=r["commence_time"], api_event_id=str(r["api_event_id"]),
                fighter_id=fighter_id, fighter_name=r[f"fighter_{side}"],
                opponent_id=r[f"fighter_{other}_id"], opponent_name=r[f"fighter_{other}"],
                bookmaker=r[f"best_book_{side}"], p_model=float(p_model), p_market=float(p_mkt),
                p_used=float(rec.p_blended), ev=float(rec.ev_per_pound),
            ))
    planned.sort(key=lambda b: b.ev, reverse=True)
    return planned


def _record(svc: BankrollService, plan: list[PlannedBet], actor: str) -> list[PlannedBet]:
    for b in plan:
        svc.upsert_event(b.proposal.event_id, b.event_name, b.event_starts_at)
        try:
            res = svc.place_bet(b.proposal, actor=actor)
        except RiskRejected as exc:
            b.status, b.reasons = "rejected", exc.reasons
            continue
        b.bet_id = res.bet_id
        b.status = "recorded" if res.created else "already recorded"
        save_context(svc.conn, b)
    return plan


def dry_run(svc: BankrollService, plan: list[PlannedBet]) -> list[PlannedBet]:
    """Ask the real risk engine about every proposal, on a scratch copy of the ledger."""
    with tempfile.TemporaryDirectory() as d:
        scratch = svc.scratch_copy(Path(d) / "scratch.db")
        try:
            for b in _record(scratch, plan, actor="agent"):
                if b.status in ("recorded", "already recorded"):
                    b.status = "approved" if b.status == "recorded" else "already recorded"
                    b.bet_id = None
        finally:
            scratch.close()
    return plan


def record_paper_bets(svc: BankrollService, plan: list[PlannedBet]) -> list[PlannedBet]:
    if svc.mode != "paper":
        raise UfcEdgeError("Automatic recording is only allowed in paper mode.")
    return _record(svc, plan, actor="agent")


def save_context(conn: sqlite3.Connection, b: PlannedBet, beyond_limits: str | None = None) -> None:
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO bet_context(bet_id, api_event_id, commence_time, fighter_id, fighter_name,
                 opponent_id, opponent_name, p_model, p_market, ev_at_bet, bookmaker, beyond_limits)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (b.bet_id, b.api_event_id, b.commence_time, b.fighter_id, b.fighter_name, b.opponent_id,
             b.opponent_name, b.p_model, b.p_market, b.ev, b.bookmaker, beyond_limits),
        )


def experiment_plan(preds: pd.DataFrame, svc: BankrollService, n: int = 5, min_p: float = 0.70) -> list[PlannedBet]:
    """PAPER EXPERIMENT: the n "safest" bets on the next card, ignoring your stake and exposure limits.

    Safest = the model's favourite at min_p or better, where the best odds still show an edge of zero
    or more. The whole active bankroll is split equally across them.
    """
    if preds.empty:
        return []
    preds = preds.copy()
    preds["_card"] = _cards(preds)
    preds = preds[preds["_card"] == preds.loc[pd.to_datetime(preds["commence_time"], utc=True).idxmin(), "_card"]]
    start = pd.Timestamp(preds["commence_time"].min()).to_pydatetime()
    picks = []
    for _, r in preds.iterrows():
        side = 1 if r["p_model_1"] >= 0.5 else 2
        p = r["p_model_1"] if side == 1 else 1 - r["p_model_1"]
        odds = float(r[f"best_odds_{side}"])
        ev = p * (odds - 1) - (1 - p)
        if p >= min_p and ev >= 0:
            picks.append((ev, side, p, odds, r))
    picks.sort(key=lambda x: x[0], reverse=True)
    picks = picks[:n]
    if not picks:
        return []
    stake = svc.balances().active_pence // len(picks)
    out = []
    for ev, side, p, odds, r in picks:
        other = 2 if side == 1 else 1
        p_mkt = r["p_market_1"] if side == 1 else 1 - r["p_market_1"]
        out.append(PlannedBet(
            proposal=BetProposal(
                idempotency_key=f"{r['api_event_id']}:{r[f'fighter_{side}_id']}", event_id=f"card:{start:%Y-%m-%d}",
                fight_id=str(r["api_event_id"]), market="moneyline", selection=r[f"fighter_{side}"],
                decimal_odds=Decimal(f"{odds:.2f}"), stake_pence=int(stake),
                odds_source=f"The Odds API / {r[f'best_book_{side}']}",
                odds_captured_at=pd.Timestamp(r["odds_captured_at"]).to_pydatetime(),
                model_probability=float(p), market_probability=float(min(max(p_mkt, 1e-4), 1 - 1e-4)),
            ),
            event_name=f"UFC card {start:%a %d %b %Y}", event_starts_at=start, commence_time=r["commence_time"],
            api_event_id=str(r["api_event_id"]), fighter_id=r[f"fighter_{side}_id"], fighter_name=r[f"fighter_{side}"],
            opponent_id=r[f"fighter_{other}_id"], opponent_name=r[f"fighter_{other}"], bookmaker=r[f"best_book_{side}"],
            p_model=float(p), p_market=float(p_mkt), p_used=float(p), ev=float(ev),
        ))
    return out


def record_experiment(svc: BankrollService, plan: list[PlannedBet]) -> list[PlannedBet]:
    """Record an experiment plan on the PAPER ledger, skipping only the limit checks."""
    if svc.mode != "paper":
        raise UfcEdgeError("Experiments are only allowed on the paper ledger.")
    for b in plan:
        svc.upsert_event(b.proposal.event_id, b.event_name, b.event_starts_at)
        try:
            res = svc.place_bet(b.proposal, actor="operator", beyond_limits=True)
        except RiskRejected as exc:
            b.status, b.reasons = "rejected", exc.reasons
            continue
        b.bet_id = res.bet_id
        b.status = "recorded" if res.created else "already recorded"
        b.reasons = list(res.overridden)
        save_context(svc.conn, b, beyond_limits="; ".join(res.overridden) or "experiment")
    return plan


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
