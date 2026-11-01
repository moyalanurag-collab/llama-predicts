"""Plain-English evaluation report (Markdown) for the walk-forward results."""

from __future__ import annotations

import pandas as pd

from .walkforward import WalkForward, betting_preview, calibration_table, score_table


def _table(df: pd.DataFrame, fmt: dict[str, str]) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(fmt.get(c, "{}").format(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def build_report(wf: WalkForward, trained: dict | None = None) -> tuple[str, dict]:
    P = wf.predictions
    names = {"market_p_a": "Market (bookmaker odds, margin removed)", "p_elo": "Elo rating only",
             "p_logreg": "Logistic regression (stats)", "p_stats": "Gradient boosting (stats)",
             "p_final": "**Final: stats blended with market**"}
    scores = score_table(P, names)
    stats_only = score_table(P, {"p_elo": "Elo rating only", "p_logreg": "Logistic regression (stats)",
                                 "p_stats": "Gradient boosting (stats)"})
    calib = calibration_table(P, "p_final")
    prev = betting_preview(P, "p_final")
    prev_stats = betting_preview(P, "p_stats")
    mkt = scores.set_index("model")
    m_ll = mkt.loc[names["market_p_a"], "log_loss"]
    f_ll = mkt.loc[names["p_final"], "log_loss"]
    w_m, w_s = list(wf.blend_weights.values())[-1] if wf.blend_weights else (1.0, 0.0)

    summary = {
        "market_log_loss": float(m_ll), "final_log_loss": float(f_ll),
        "beats_market": bool(f_ll < m_ll), "blend_weights": (w_m, w_s),
        "preview_roi": prev.get("roi"), "preview_ci": (prev.get("ci_low"), prev.get("ci_high")),
        "preview_bets": prev.get("bets"),
    }
    ci_crosses_zero = prev.get("bets", 0) and prev["ci_low"] < 0 < prev["ci_high"]
    verdict = (
        "The blended model is **slightly better than the market** on log-loss"
        if f_ll < m_ll else "The blended model is **not better than the market**"
    )
    first, last = int(P["year"].min()), int(P["year"].max())

    md = [
        "# Model evaluation report",
        "",
        f"Walk-forward test, {first}–{last}: each year is predicted by models trained only on earlier years.",
        "Features use only information from before each fight. Odds are approximate closing lines from the",
        "Kaggle dataset (no timestamp or bookmaker), so treat every betting number here as indicative.",
        "",
        "## Headline",
        "",
        f"- {verdict}: {f_ll:.4f} vs {m_ll:.4f} (lower is better; a coin flip scores 0.6931).",
        f"- Learned blend weights (most recent year): market {w_m:.2f}, stats model {w_s:.2f}. "
        "The market carries most of the information; the stats model adds a little.",
    ]
    if prev.get("bets"):
        md.append(
            f"- Betting preview (flat £1 stakes, min edge 3%, odds 1.20–4.00): {prev['bets']} bets from "
            f"{prev['fights']} fights, ROI {prev['roi']:+.1%} (95% range {prev['ci_low']:+.1%} to {prev['ci_high']:+.1%})."
        )
        md.append(
            "- **This is not proof of an edge.** The range includes zero, so luck can't be ruled out."
            if ci_crosses_zero else
            "- The range excludes zero, but several model variants were tried, which flatters the best one. Confirm with paper trading."
        )
    if prev_stats.get("bets"):
        md.append(
            f"- For contrast, betting on the stats model *without* the market blend: {prev_stats['bets']} bets, "
            f"ROI {prev_stats['roi']:+.1%}. Trusting the model too much loses money."
        )
    md += [
        "",
        "## Accuracy against the market",
        "",
        "Fights that have market odds:",
        "",
        _table(scores, {"fights": "{:,}", "log_loss": "{:.4f}", "brier": "{:.4f}", "accuracy": "{:.1%}"}),
        "",
        "All fights (stats models only; includes fights with no odds):",
        "",
        _table(stats_only, {"fights": "{:,}", "log_loss": "{:.4f}", "brier": "{:.4f}", "accuracy": "{:.1%}"}),
        "",
        "## Calibration of the final model",
        "",
        "When the model says the favourite wins X% of the time, how often did they?",
        "",
        _table(calib.assign(bin=calib["bin"].astype(str)),
               {"fights": "{:,}", "predicted": "{:.1%}", "actual": "{:.1%}"}),
    ]
    if prev.get("bets"):
        bp = prev["by_period"].reset_index().rename(columns={"year": "period"})
        md += ["", "## Betting preview by period", "", _table(bp, {"bets": "{:,}", "roi": "{:+.1%}"})]
    if wf.method_log_loss:
        ml = wf.method_log_loss
        md += ["", "## Method of victory", "",
               f"Six outcomes (each fighter by KO/TKO, submission or decision), {ml['n']:,} fights: "
               f"log-loss {ml['model']:.3f} vs {ml['base_rates']:.3f} for historical base rates. "
               "It's reported for context only; method markets carry bigger margins and aren't bet in v1."]
    if trained:
        md += ["", "## Saved model", "",
               f"Trained {trained['trained_at']} on {trained['training_fights']:,} fights up to {trained['data_through']}."]
    md += ["", "Past results don't guarantee future performance. Backtests can overstate edges, so",
           "paper trading against real, timestamped UK odds is the real test.", ""]
    return "\n".join(md), summary
