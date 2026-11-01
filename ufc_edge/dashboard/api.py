"""Data for the dashboard. Every function returns plain JSON-ready dicts.

The dashboard reads the same databases and reports as the command line. It
can do only what the operator can safely do from a browser:

* engage the kill switch (stopping never needs authorisation);
* release it, set up a bankroll, or record a bet. Each needs the exact typed
  confirmation phrase, and each goes through the same ledger and risk engine
  as everything else.

Risk-setting changes stay on the command line on purpose.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, fields
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import RiskConfig
from ..errors import RiskRejected, UfcEdgeError
from ..ledger import BankrollService, ManualAuthorisation, bankroll_phrase
from ..money import pounds_to_pence


class Paths:
    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data = Path(data_dir)
        self.stats_db = self.data / "ufc.db"
        self.reports = self.data / "reports"
        self.model = self.data / "models" / "ufc_model.pkl"

    def ledger(self, mode: str) -> Path:
        if mode not in ("paper", "live"):
            raise UfcEdgeError("mode must be paper or live")
        return self.data / f"{mode}.db"


def clean(obj: Any) -> Any:
    """Make pandas, numpy and NaN values JSON-safe."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, (Decimal,)):
        return float(obj)
    if hasattr(obj, "item"):  # numpy scalar
        return clean(obj.item())
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return obj


def _svc(paths: Paths, mode: str) -> BankrollService:
    paths.data.mkdir(parents=True, exist_ok=True)
    return BankrollService(paths.ledger(mode), mode=mode)


def _stats(paths: Paths) -> sqlite3.Connection | None:
    if not paths.stats_db.exists():
        return None
    conn = sqlite3.connect(str(paths.stats_db))
    conn.row_factory = sqlite3.Row
    return conn


def _predictions(paths: Paths) -> pd.DataFrame:
    p = paths.reports / "upcoming_predictions.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    needed = {"commence_time", "api_event_id", "fighter_1_id", "fighter_2_id", "p_model_1", "odds_captured_at"}
    if not needed.issubset(df.columns):
        return pd.DataFrame()  # made by an older version; `predict` or `weekly` rewrites it
    return df[pd.to_datetime(df["commence_time"], utc=True) > pd.Timestamp.now(tz="UTC")]


# ------------------------------------------------------------------ overview
def balance_history(svc: BankrollService) -> list[dict]:
    rows = svc.conn.execute(
        """SELECT t.id, t.created_at, t.kind, l.account, l.amount_pence
           FROM ledger_transactions t JOIN ledger_lines l ON l.txn_id = t.id ORDER BY t.id, l.id""").fetchall()
    bal = {"ACTIVE": 0, "OPEN_STAKES": 0, "PROTECTED": 0, "WITHDRAWN": 0, "MARKET": 0}
    out: list[dict] = []
    last_txn = None
    for r in rows:
        if r["account"] in bal:
            bal[r["account"]] += r["amount_pence"]
        if r["id"] != last_txn:
            out.append({})
            last_txn = r["id"]
        out[-1] = {"id": r["id"], "at": r["created_at"], "kind": r["kind"], "active": bal["ACTIVE"], "open": bal["OPEN_STAKES"],
                   "protected": bal["PROTECTED"], "withdrawn": bal["WITHDRAWN"], "pnl": -bal["MARKET"]}
    return out


def modes(paths: Paths) -> dict:
    out = {}
    for m in ("paper", "live"):
        if paths.ledger(m).exists():
            svc = _svc(paths, m)
            try:
                out[m] = svc.status()["status"]
            finally:
                svc.close()
        else:
            out[m] = "UNINITIALISED"
    return out


def freshness(paths: Paths) -> dict:
    info: dict[str, Any] = {"newest_result": None, "odds_captured_at": None, "model_trained_at": None,
                            "model_data_through": None}
    conn = _stats(paths)
    if conn:
        try:
            info["newest_result"] = conn.execute("SELECT MAX(date) FROM fights").fetchone()[0]
            info["odds_captured_at"] = conn.execute("SELECT MAX(captured_at) FROM odds_snapshots").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    if paths.model.exists():
        try:
            info.update(_model_info(paths.model))
        except Exception as exc:  # noqa: BLE001
            info["model_error"] = str(exc)
    return info


_MODEL_CACHE: dict[str, tuple[float, dict]] = {}


def _model_info(path: Path) -> dict:
    """Model metadata, cached until the model file changes (loading it takes a second or two)."""
    mtime = path.stat().st_mtime
    hit = _MODEL_CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    from ..model.artifact import load

    b = load(path)
    info = {"model_trained_at": b.trained_at, "model_data_through": b.data_through, "model_summary": b.evaluation}
    _MODEL_CACHE[str(path)] = (mtime, info)
    return info


def overview(paths: Paths, mode: str) -> dict:
    svc = _svc(paths, mode)
    try:
        status = svc.status()
        cfg = svc.risk_config()
        history = balance_history(svc)
        open_bets = _bets(svc, open_only=True)
        recent = _bets(svc)[:8]
        settled = [b for b in _bets(svc) if b["status"] in ("WON", "LOST", "VOID")]
    finally:
        svc.close()
    preds = _predictions(paths)
    card = None
    if not preds.empty:
        card = {"fights": len(preds), "first": preds["commence_time"].min(), "last": preds["commence_time"].max(),
                "value_flags": int((preds["value_flag"].fillna("") != "").sum())}
    return clean({"mode": mode, "modes": modes(paths), "status": status, "config": asdict(cfg),
                  "history": history, "open_bets": open_bets, "recent_bets": recent,
                  "record": _record_stats(settled), "card": card, "freshness": freshness(paths)})


def _record_stats(settled: list[dict]) -> dict:
    graded = [b for b in settled if b["status"] in ("WON", "LOST")]
    staked = sum(b["stake_pence"] for b in graded)
    pnl = sum((b["returned_pence"] or 0) - b["stake_pence"] for b in graded)
    clv = [b["clv"] for b in graded if b.get("clv") is not None]
    return {"settled": len(settled), "graded": len(graded), "won": sum(b["status"] == "WON" for b in graded),
            "staked_pence": staked, "pnl_pence": pnl, "roi": (pnl / staked) if staked else None,
            "avg_clv": (sum(clv) / len(clv)) if clv else None, "clv_bets": len(clv)}


def _bets(svc: BankrollService, open_only: bool = False) -> list[dict]:
    where = "WHERE b.status = 'OPEN'" if open_only else ""
    rows = svc.conn.execute(
        f"""SELECT b.id, b.event_id, b.selection, b.decimal_odds, b.stake_pence, b.status, b.placed_at,
                   b.settled_at, b.returned_pence, b.odds_source, b.model_probability,
                   c.opponent_name, c.commence_time, c.p_market, c.ev_at_bet, c.closing_odds, c.clv, c.bookmaker,
                   c.beyond_limits
            FROM bets b LEFT JOIN bet_context c ON c.bet_id = b.id {where} ORDER BY b.id DESC""").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ fight card
def card(paths: Paths) -> dict:
    preds = _predictions(paths)
    if preds.empty:
        return {"fights": [], "captured_at": None}
    preds = preds.sort_values("commence_time")
    return clean({"fights": preds.to_dict(orient="records"),
                  "captured_at": preds["odds_captured_at"].max(), "freshness": freshness(paths)})


# ------------------------------------------------------------------ betting plan
def plan(paths: Paths, mode: str) -> dict:
    from ..strategy import build_plan, dry_run

    preds = _predictions(paths)
    svc = _svc(paths, mode)
    try:
        status = svc.status()
        cfg = svc.risk_config()
        if preds.empty or status["status"] == "UNINITIALISED":
            proposals = []
        else:
            proposals = [b.to_dict() for b in dry_run(svc, build_plan(preds, svc, cfg))]
        recorded_keys = {r[0] for r in svc.conn.execute("SELECT idempotency_key FROM bets")}
    finally:
        svc.close()
    for p in proposals:
        p["already_recorded"] = p["idempotency_key"] in recorded_keys
    approved = [p for p in proposals if p["status"] == "approved" and not p.get("already_recorded")]
    odds_age = None
    if not preds.empty:
        odds_age = (pd.Timestamp.now(tz="UTC") - pd.to_datetime(preds["odds_captured_at"], utc=True).max()).total_seconds() / 60
    return clean({
        "mode": mode, "status": status, "config": asdict(cfg), "proposals": proposals,
        "approved_stake_pence": sum(p["stake_pence"] for p in approved),
        "approved_return_pence": sum(p["potential_return_pence"] for p in approved),
        "odds_age_minutes": odds_age, "has_predictions": not preds.empty,
    })


def record_bet(paths: Paths, mode: str, key: str, odds: str, stake_pounds: str,
               confirmation: str) -> dict:
    """Record a bet the operator actually placed (live) or wants on the paper record."""
    from ..strategy import build_plan, save_context

    expected = "I PLACED THIS BET" if mode == "live" else "RECORD PAPER BET"
    if confirmation.strip() != expected:
        raise UfcEdgeError(f'Type "{expected}" to confirm.')
    preds = _predictions(paths)
    svc = _svc(paths, mode)
    try:
        match = [b for b in build_plan(preds, svc) if b.proposal.idempotency_key == key]
        if not match:
            raise UfcEdgeError("That proposal is no longer in the plan. Refresh the plan.")
        b = match[0]
        odds_d = Decimal(odds)
        stake = pounds_to_pence(stake_pounds)
        if stake > b.proposal.stake_pence:
            raise UfcEdgeError(f"Stake can't exceed the planned {b.proposal.stake_pence / 100:.2f}.")
        from dataclasses import replace

        b.proposal = replace(b.proposal, decimal_odds=odds_d, stake_pence=stake)
        svc.upsert_event(b.proposal.event_id, b.event_name, b.event_starts_at)
        try:
            res = svc.place_bet(b.proposal, actor="operator")
        except RiskRejected as exc:
            return {"ok": False, "reasons": exc.reasons}
        b.bet_id = res.bet_id
        save_context(svc.conn, b)
        return {"ok": True, "bet_id": res.bet_id, "created": res.created}
    finally:
        svc.close()


# ------------------------------------------------------------------ bankroll
def bankroll(paths: Paths, mode: str) -> dict:
    svc = _svc(paths, mode)
    try:
        rows = svc.conn.execute(
            """SELECT t.id, t.created_at, t.kind, t.memo, t.bet_id, l.account, l.amount_pence
               FROM ledger_transactions t JOIN ledger_lines l ON l.txn_id = t.id ORDER BY t.id DESC, l.id""").fetchall()
        txns: dict[int, dict] = {}
        for r in rows:
            t = txns.setdefault(r["id"], {"id": r["id"], "at": r["created_at"], "kind": r["kind"], "memo": r["memo"],
                                          "bet_id": r["bet_id"], "lines": {}})
            t["lines"][r["account"]] = r["amount_pence"]
        out = {"status": svc.status(), "history": balance_history(svc), "transactions": list(txns.values())[:200],
               "bets": _bets(svc)[:200]}
    finally:
        svc.close()
    return clean(out)


# ------------------------------------------------------------------ performance
def performance(paths: Paths, mode: str) -> dict:
    from ..model.walkforward import calibration_table, score_table

    out: dict[str, Any] = {}
    wf = paths.reports / "walkforward_predictions.csv"
    if wf.exists():
        P = pd.read_csv(wf)
        scores = score_table(P, {"market_p_a": "Market", "p_elo": "Elo only", "p_logreg": "Logistic (stats)",
                                 "p_stats": "Gradient boosting (stats)", "p_final": "Final (blended)"})
        cal = calibration_table(P, "p_final")
        cal["bin"] = cal["bin"].astype(str)
        P["year"] = P["year"].astype(int)
        by_year = []
        for y, g in P.dropna(subset=["p_final", "market_p_a"]).groupby("year"):
            from ..model.core import log_loss

            by_year.append({"year": int(y), "fights": len(g), "market": log_loss(g["market_p_a"], g["a_wins"]),
                            "model": log_loss(g["p_final"], g["a_wins"])})
        out["model"] = {"scores": scores.to_dict(orient="records"), "calibration": cal.to_dict(orient="records"),
                        "by_year": by_year}
    bt = paths.reports / "backtest.json"
    if bt.exists():
        out["backtest"] = json.loads(bt.read_text(encoding="utf-8"))
    svc = _svc(paths, mode)
    try:
        settled = [b for b in _bets(svc) if b["status"] in ("WON", "LOST", "VOID")]
    finally:
        svc.close()
    out["record"] = _record_stats(settled)
    out["settled_bets"] = settled[:100]
    out["freshness"] = freshness(paths)
    return clean(out)


# ------------------------------------------------------------------ settings & controls
CONFIG_HELP = {
    "kelly_fraction": "Fraction of the Kelly stake used. Lower = smaller, steadier stakes.",
    "model_weight": "How far the bet's probability leans on the model rather than the market price.",
    "min_edge": "Minimum expected profit per £1 staked before a bet is considered.",
    "max_stake_fraction": "Largest single stake, as a share of money in play.",
    "min_stake_pence": "Smallest stake your bookmaker accepts.",
    "max_event_exposure_fraction": "Most money that can be riding on one event.",
    "max_total_exposure_fraction": "Most money that can be riding on all open bets.",
    "max_daily_loss_fraction": "Worst-case loss allowed in any 24 hours, as a share of the £40 cap.",
    "max_event_loss_fraction": "Worst-case loss allowed on one event, as a share of the £40 cap.",
    "max_bets_per_event": "Most bets on a single event.",
    "min_active_bankroll_pence": "Below this, no new bets; the system stops once nothing is open.",
    "max_odds_age_seconds": "Odds older than this are refused as stale.",
    "min_decimal_odds": "Shortest price considered.",
    "max_decimal_odds": "Longest price considered.",
    "allowed_markets": "Markets the system may bet.",
    "execution_mode": "Manual only: you place every real bet yourself.",
}


def settings(paths: Paths, mode: str) -> dict:
    svc = _svc(paths, mode)
    try:
        cfg = svc.risk_config()
        auths = [dict(r) for r in svc.conn.execute(
            "SELECT created_at, kind, operator, reason FROM authorisations ORDER BY id DESC LIMIT 30")]
        audit = [dict(r) for r in svc.conn.execute(
            "SELECT created_at, actor, action, payload FROM audit_log ORDER BY id DESC LIMIT 40")]
        status = svc.status()
    finally:
        svc.close()
    bt_counts = None
    bt = paths.reports / "backtest.json"
    if bt.exists():
        try:
            v = json.loads(bt.read_text(encoding="utf-8"))["variants"]
            bt_counts = {k: x["summary"]["bets"] for k, x in v.items()}
        except (KeyError, ValueError):
            bt_counts = None
    return clean({"mode": mode, "status": status, "backtest_bets": bt_counts,
                  "config": [{"key": f.name, "value": getattr(cfg, f.name), "help": CONFIG_HELP.get(f.name, "")}
                             for f in fields(RiskConfig)],
                  "authorisations": auths, "audit": audit, "bankroll_phrase_example": bankroll_phrase(4000)})


def kill(paths: Paths, mode: str, reason: str) -> dict:
    svc = _svc(paths, mode)
    try:
        svc.engage_kill_switch(reason or "Stopped from the dashboard", actor="operator")
        return {"ok": True}
    finally:
        svc.close()


def release(paths: Paths, mode: str, operator: str, reason: str, confirmation: str) -> dict:
    svc = _svc(paths, mode)
    try:
        svc.release_kill_switch(ManualAuthorisation(operator, reason, confirmation))
        return {"ok": True}
    finally:
        svc.close()


def init_bankroll(paths: Paths, mode: str, pounds: str, operator: str, reason: str, confirmation: str) -> dict:
    svc = _svc(paths, mode)
    try:
        svc.initialise_bankroll(pounds_to_pence(pounds), ManualAuthorisation(operator, reason, confirmation))
        return {"ok": True}
    finally:
        svc.close()


def refresh(paths: Paths) -> dict:
    """Take a fresh odds snapshot and re-run predictions (uses one Odds API request)."""
    from ..data.odds_api import OddsClient, load_api_key, take_snapshot
    from ..model.artifact import load
    from ..model.upcoming import predict_card

    if not paths.model.exists():
        raise UfcEdgeError("No trained model yet. Run: python -m ufc_edge train")
    conn = sqlite3.connect(str(paths.stats_db))
    try:
        snap = take_snapshot(conn, OddsClient(load_api_key()), raw_dir=paths.data / "raw" / "odds")
        preds = predict_card(conn, load(paths.model))
    finally:
        conn.close()
    paths.reports.mkdir(parents=True, exist_ok=True)
    if not preds.empty:
        preds.to_csv(paths.reports / "upcoming_predictions.csv", index=False)
    return {"ok": True, "fights": len(preds), "requests_remaining": snap.requests_remaining,
            "captured_at": snap.captured_at, "at": datetime.now(timezone.utc).isoformat()}
