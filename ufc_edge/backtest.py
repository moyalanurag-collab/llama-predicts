"""Historical backtest through the real strategy, risk engine and ledger.

Each historical event is replayed in date order:

1. The clock is set to fight day. The strategy proposes bets from that year's
   *walk-forward* predictions, made by models that never saw that year.
2. Every proposal goes through the real ``place_bet``, so stake caps,
   exposure, daily and event loss limits, bet counts and minimum edge all apply.
3. The next morning the bets settle. The ledger sweeps profit above £40 into
   protected funds, and the exhaustion latch stops everything if the bankroll runs out.

Limits of the test
------------------
The odds are the Kaggle dataset's approximate closing lines, with no
timestamp or bookmaker. Real bets are placed earlier, at whatever price your
bookmaker offers, which may be better or worse. Results are indicative, not a forecast.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import RiskConfig
from .ledger import BankrollService, ManualAuthorisation, bankroll_phrase
from .strategy import _record, build_plan

VARIANTS = {
    "current": ("Your current settings", {}),
    "trust_blend": ("Trust the model's market-blended probability (model weight 1.0)", {"model_weight": 1.0}),
}


def _drawdown(series: pd.Series) -> tuple[float, str | None]:
    peak = series.cummax()
    dd = series - peak
    if dd.empty:
        return 0.0, None
    i = dd.idxmin()
    return float(dd.min()), str(i)


def _streak(results: list[float]) -> int:
    worst = cur = 0
    for r in results:
        cur = cur + 1 if r < 0 else 0
        worst = max(worst, cur)
    return worst


def run_backtest(preds: pd.DataFrame, cfg: RiskConfig, bankroll_pence: int = 4000,
                 db_path: str | Path | None = None) -> dict:
    """``preds`` needs: fight_id, event_id, date, a_name, b_name, a_wins, market_p_a, a_odds, b_odds, p_final."""
    d = preds.dropna(subset=["p_final", "market_p_a", "a_odds", "b_odds", "a_wins"]).copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["date", "fight_id"])
    clock = {"now": datetime(2000, 1, 1, tzinfo=timezone.utc)}
    tmp = None
    if db_path is None:
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "backtest.db"
    svc = BankrollService(db_path, mode="paper", clock=lambda: clock["now"])
    svc.initialise_bankroll(bankroll_pence, ManualAuthorisation("backtest", "simulation", bankroll_phrase(bankroll_pence)))
    svc.update_risk_config(cfg, ManualAuthorisation("backtest", "simulation", "AUTHORISE RISK CHANGE"))

    curve, bets, exhausted_on = [], [], None
    rejected: dict[str, int] = {}
    for event_id, ev in d.groupby("event_id", sort=False):
        day = ev["date"].iloc[0].to_pydatetime().replace(tzinfo=timezone.utc)
        if svc.status()["status"] == "EXHAUSTED":
            exhausted_on = exhausted_on or str(day.date())
            break
        clock["now"] = day.replace(hour=12)
        start = day.replace(hour=23)
        rows = pd.DataFrame({
            "commence_time": start.isoformat(), "api_event_id": ev["fight_id"].to_numpy(),
            "fighter_1": ev["a_name"].fillna("A").to_numpy(), "fighter_2": ev["b_name"].fillna("B").to_numpy(),
            "fighter_1_id": ("A:" + ev["fight_id"]).to_numpy(), "fighter_2_id": ("B:" + ev["fight_id"]).to_numpy(),
            "p_model_1": ev["p_final"].to_numpy(), "p_market_1": ev["market_p_a"].to_numpy(),
            "best_odds_1": ev["a_odds"].round(2).to_numpy(), "best_odds_2": ev["b_odds"].round(2).to_numpy(),
            "best_book_1": "historical close", "best_book_2": "historical close",
            "odds_captured_at": clock["now"].isoformat(),
        })
        plan = _record(svc, build_plan(rows, svc), actor="agent")
        for b in plan:
            if b.status == "rejected":
                for r in b.reasons:
                    code = r.split(":")[0]
                    rejected[code] = rejected.get(code, 0) + 1
        clock["now"] = (day + timedelta(days=1)).replace(hour=6)
        outcome = dict(zip(ev["fight_id"], ev["a_wins"]))
        for b in plan:
            if b.bet_id is None:
                continue
            a_won = outcome[b.api_event_id] == 1
            won = a_won if b.fighter_id.startswith("A:") else not a_won
            svc.settle_bet(b.bet_id, "WON" if won else "LOST", "historical result", verified=True, actor="system")
            stake = b.proposal.stake_pence
            pnl = (int(stake * float(b.proposal.decimal_odds)) - stake) if won else -stake
            bets.append({"date": str(day.date()), "event_id": event_id, "selection": b.fighter_name,
                         "opponent": b.opponent_name, "odds": float(b.proposal.decimal_odds),
                         "stake_pence": stake, "pnl_pence": pnl, "won": won, "ev": b.ev})
        bal = svc.balances()
        curve.append({"date": str(day.date()), "active": bal.active_pence, "protected": bal.protected_pence,
                      "open": bal.open_stakes_pence, "total": bal.total_funds_pence})
    status = svc.status()
    svc.verify_integrity()
    svc.close()
    if tmp:
        tmp.cleanup()

    b = pd.DataFrame(bets)
    c = pd.DataFrame(curve)
    summary = {
        "bets": len(b), "events": len(c),
        "first_date": c["date"].iloc[0] if len(c) else None, "last_date": c["date"].iloc[-1] if len(c) else None,
        "staked_pence": int(b["stake_pence"].sum()) if len(b) else 0,
        "pnl_pence": int(b["pnl_pence"].sum()) if len(b) else 0,
        "final_active_pence": status["active_pence"], "final_protected_pence": status["protected_pence"],
        "final_total_pence": status["active_pence"] + status["protected_pence"] + status["open_stakes_pence"],
        "status": status["status"], "exhausted_on": exhausted_on,
        "rejections": dict(sorted(rejected.items(), key=lambda kv: -kv[1])),
    }
    if len(b):
        summary.update(
            roi_on_stake=float(b["pnl_pence"].sum() / b["stake_pence"].sum()),
            win_rate=float(b["won"].mean()), avg_odds=float(b["odds"].mean()),
            avg_stake_pence=float(b["stake_pence"].mean()), longest_losing_streak=_streak(b["pnl_pence"].tolist()),
        )
        dd, dd_date = _drawdown(c.set_index("date")["total"])
        summary.update(max_drawdown_pence=int(dd), max_drawdown_date=dd_date)
        b["year"] = b["date"].str[:4]
        by_year = b.groupby("year").agg(bets=("pnl_pence", "size"), staked=("stake_pence", "sum"),
                                        pnl=("pnl_pence", "sum")).reset_index()
        by_year["roi"] = by_year["pnl"] / by_year["staked"]
        band = pd.cut(b["odds"], [1.0, 1.6, 2.2, 3.0, 4.01], labels=["1.20–1.60", "1.61–2.20", "2.21–3.00", "3.01–4.00"])
        by_band = b.groupby(band, observed=True).agg(bets=("pnl_pence", "size"), staked=("stake_pence", "sum"),
                                                     pnl=("pnl_pence", "sum")).reset_index(names="odds")
        by_band["roi"] = by_band["pnl"] / by_band["staked"]
        rng = np.random.default_rng(0)
        ratios = [(lambda s: s["pnl_pence"].sum() / s["stake_pence"].sum())(b.sample(len(b), replace=True, random_state=int(x)))
                  for x in rng.integers(0, 1_000_000, 1000)]
        summary.update(roi_ci_low=float(np.percentile(ratios, 2.5)), roi_ci_high=float(np.percentile(ratios, 97.5)))
    else:
        by_year = by_band = pd.DataFrame()
    return {
        "summary": summary,
        "curve": curve,
        "by_year": by_year.to_dict(orient="records") if len(by_year) else [],
        "by_odds": by_band.astype({"odds": str}).to_dict(orient="records") if len(by_band) else [],
        "bets": bets[-300:],
    }


def run_all_variants(preds: pd.DataFrame, base: RiskConfig, bankroll_pence: int = 4000, log=print) -> dict:
    out = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "bankroll_pence": bankroll_pence, "variants": {}}
    for key, (label, changes) in VARIANTS.items():
        cfg = replace(base, **changes)
        log(f"  backtest: {label} ...")
        res = run_backtest(preds, cfg, bankroll_pence)
        res["label"] = label
        res["config"] = json.loads(cfg.to_json())
        out["variants"][key] = res
    return out
