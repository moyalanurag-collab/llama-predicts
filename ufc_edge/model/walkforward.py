"""Walk-forward evaluation: the honest test of the model.

For each test year Y, every model is trained only on fights *before* 1 January
of Y and then scored on year Y. Nothing from Y or later is ever seen in
training. Features are already point-in-time (see features/build.py), so each
prediction uses only information available before that fight.

The market blend is walked forward too. Its weights for year Y are fitted only
on out-of-sample predictions from earlier years.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from ..config import RiskConfig
from .core import METHOD_CLASSES, Explainer, MarketBlend, MethodModel, StatsModel, log_loss, method_labels


@dataclass
class WalkForward:
    predictions: pd.DataFrame
    blend_weights: dict[int, tuple[float, float]] = field(default_factory=dict)
    method_log_loss: dict[str, float] = field(default_factory=dict)


def labelled(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df["a_wins"].notna() & (df["is_upcoming"] == 0)].copy()
    d["year"] = d["date"].dt.year
    return d


def _elo_baseline(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    x = train[["diff_elo"]].fillna(0).to_numpy()
    y = train["a_wins"].to_numpy()
    lr = LogisticRegression(fit_intercept=False).fit(np.r_[x, -x], np.r_[y, 1 - y])
    return lr.predict_proba(test[["diff_elo"]].fillna(0).to_numpy())[:, 1]


def walk_forward(
    df: pd.DataFrame,
    first_test_year: int = 2012,
    blend_warmup_years: int = 2,
    with_methods: bool = True,
    log: Callable[[str], None] = lambda _: None,
) -> WalkForward:
    data = labelled(df)
    years = sorted(y for y in data["year"].unique() if y >= first_test_year)
    parts = []
    method_parts = []
    for year in years:
        train = data[data["year"] < year]
        test = data[data["year"] == year]
        if len(train) < 500 or test.empty:
            continue
        # The guarantee this whole evaluation rests on:
        assert train["date"].max() < test["date"].min(), "training data overlaps the test year"
        log(f"  {year}: training on {len(train)} fights, testing on {len(test)}")
        stats = StatsModel().fit(train)
        expl = Explainer().fit(train)
        cols = ["fight_id", "date", "year", "a_wins", "method_group", "a_name", "b_name"]
        cols += [c for c in ("market_p_a", "a_odds", "b_odds") if c in test]
        out = test[cols].copy()
        out["p_stats"] = stats.predict(test)
        out["p_logreg"] = expl.predict(test)
        out["p_elo"] = _elo_baseline(train, test)
        parts.append(out)
        if with_methods:
            mm = MethodModel().fit(train)
            mp = mm.predict(test)
            mp["label"] = method_labels(test)
            base = method_labels(train).value_counts(normalize=True).reindex(METHOD_CLASSES).fillna(1e-3)
            mp["base_prob"] = [base.get(lbl, np.nan) if isinstance(lbl, str) else np.nan for lbl in mp["label"]]
            method_parts.append(mp)
    preds = pd.concat(parts).sort_values("date", kind="mergesort")

    # Walk-forward market blend.
    result = WalkForward(preds)
    preds["p_final"] = np.nan
    if "market_p_a" in preds:
        has_mkt = preds["market_p_a"].notna()
        for year in sorted(preds["year"].unique()):
            prior = preds[has_mkt & (preds["year"] < year)]
            if prior["year"].nunique() < blend_warmup_years:
                continue
            blend = MarketBlend().fit(prior["market_p_a"], prior["p_stats"], prior["a_wins"])
            result.blend_weights[int(year)] = (blend.w_market, blend.w_stats)
            rows = has_mkt & (preds["year"] == year)
            preds.loc[rows, "p_final"] = blend.predict(preds.loc[rows, "market_p_a"], preds.loc[rows, "p_stats"])

    if method_parts:
        mp = pd.concat(method_parts)
        mp = mp[mp["label"].notna()]
        p_true = np.array([row[lbl] for lbl, (_, row) in zip(mp["label"], mp[METHOD_CLASSES].iterrows())])
        result.method_log_loss = {
            "model": float(-np.log(np.clip(p_true, 1e-6, 1)).mean()),
            "base_rates": float(-np.log(np.clip(mp["base_prob"].to_numpy(dtype=float), 1e-6, 1)).mean()),
            "n": int(len(mp)),
        }
    return result


# ---------------------------------------------------------------- metrics
def score_table(preds: pd.DataFrame, columns: dict[str, str]) -> pd.DataFrame:
    """Log-loss, Brier and accuracy for each probability column, on fights where all are present."""
    cols = [c for c in columns if c in preds]
    d = preds.dropna(subset=cols + ["a_wins"])
    y = d["a_wins"].to_numpy()
    rows = []
    for c in cols:
        p = d[c].to_numpy()
        rows.append({"model": columns[c], "fights": len(d), "log_loss": log_loss(p, y),
                     "brier": float(((p - y) ** 2).mean()), "accuracy": float(((p > 0.5) == (y == 1)).mean())})
    return pd.DataFrame(rows)


def calibration_table(preds: pd.DataFrame, col: str) -> pd.DataFrame:
    d = preds.dropna(subset=[col, "a_wins"])
    # Fold onto the favoured side so bins run 50%-100%.
    p = np.maximum(d[col], 1 - d[col])
    won = np.where(d[col] >= 0.5, d["a_wins"], 1 - d["a_wins"])
    bins = pd.cut(p, [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.9, 1.0], include_lowest=True)
    t = pd.DataFrame({"bin": bins, "p": p, "won": won}).groupby("bin", observed=True).agg(
        fights=("p", "size"), predicted=("p", "mean"), actual=("won", "mean"))
    return t.reset_index()


def betting_preview(preds: pd.DataFrame, col: str, cfg: RiskConfig | None = None, seed: int = 0) -> dict:
    """Flat £1 stakes on every side whose EV at the listed odds clears the minimum edge.

    This is a *preview* to see whether any edge is visible. It isn't the Stage 4
    backtest, which runs the real staking rules, risk engine and ledger.
    """
    cfg = cfg or RiskConfig()
    lo, hi = float(cfg.min_decimal_odds), float(cfg.max_decimal_odds)
    d = preds.dropna(subset=[col, "a_odds", "b_odds", "a_wins"]).copy()
    pa = d[col]
    ev_a = pa * (d["a_odds"] - 1) - (1 - pa)
    ev_b = (1 - pa) * (d["b_odds"] - 1) - pa
    bet_a = (ev_a >= cfg.min_edge) & d["a_odds"].between(lo, hi)
    bet_b = (ev_b >= cfg.min_edge) & d["b_odds"].between(lo, hi) & ~bet_a
    d["pnl"] = np.where(bet_a, np.where(d["a_wins"] == 1, d["a_odds"] - 1, -1.0),
                        np.where(bet_b, np.where(d["a_wins"] == 0, d["b_odds"] - 1, -1.0), np.nan))
    b = d[d["pnl"].notna()]
    if b.empty:
        return {"bets": 0, "fights": len(d)}
    rng = np.random.default_rng(seed)
    boots = [rng.choice(b["pnl"].to_numpy(), len(b)).mean() for _ in range(2000)]
    periods = b.groupby(pd.cut(b["year"], [2009, 2013, 2016, 2019, 2022, 2030],
                               labels=["2010-13", "2014-16", "2017-19", "2020-22", "2023+"]), observed=True)
    return {
        "fights": len(d), "bets": len(b), "roi": float(b["pnl"].mean()),
        "ci_low": float(np.percentile(boots, 2.5)), "ci_high": float(np.percentile(boots, 97.5)),
        "win_rate": float((b["pnl"] > 0).mean()),
        "by_period": periods["pnl"].agg(["size", "mean"]).rename(columns={"size": "bets", "mean": "roi"}),
    }
