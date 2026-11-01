"""Predictions for upcoming fights, using the odds feed as the fight card.

UFCStats can't be scraped (see docs/DATA_SOURCES.md), so the card comes from
the latest odds snapshot. Only fights with odds can be bet anyway. Fighter
names from the feed are matched to the history database. A fighter who
can't be matched is treated as a UFC debutant and flagged. The feed covers
all MMA, so a fight where *neither* fighter is in the UFC database is assumed
not to be UFC and is skipped.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..config import RiskConfig
from ..data.names import fuzzy_unique_match, name_keys, normalise_name
from ..ev import devig_power, expected_value
from ..features.build import attach_market_odds, build_fight_features
from .artifact import ModelBundle

FACTOR_LABELS = {
    "elo": "rating (Elo)", "n_fights": "UFC experience", "n_wins": "UFC wins", "n_losses": "UFC losses",
    "win_rate": "win rate", "win_rate_last5": "recent form", "streak": "current streak",
    "days_since_last": "time since last fight", "age": "age", "height_cm": "height", "reach_cm": "reach",
    "sig_landed_pm": "strikes landed per min", "sig_absorbed_pm": "strikes absorbed per min",
    "sig_acc": "striking accuracy", "sig_def": "striking defence",
    "sig_landed_pm_last5": "recent striking output", "sig_absorbed_pm_last5": "recent strikes absorbed",
    "td_landed_per15": "takedowns per 15 min", "td_acc": "takedown accuracy", "td_def": "takedown defence",
    "sub_att_per15": "submission attempts", "kd_per15": "knockdowns landed", "kd_absorbed_per15": "knockdowns suffered",
    "ctrl_share": "control time", "ctrl_against_share": "time controlled by opponents",
    "head_share": "head-strike share", "distance_share": "distance-striking share",
    "clinch_share": "clinch-striking share", "ground_share": "ground-striking share",
    "ko_win_rate": "KO wins", "sub_win_rate": "submission wins", "dec_win_rate": "decision wins",
    "ko_loss_rate": "KO losses", "sub_loss_rate": "submission losses", "finish_rate": "finishing rate",
    "avg_fight_min": "average fight length", "five_round_exp": "5-round experience",
    "title_fight_exp": "title-fight experience", "late_output_ratio": "late-round output",
    "opp_elo_avg": "strength of past opponents", "minutes_with_stats": "cage time",
}


def _num(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


def style_tags(r: pd.Series, p: str) -> list[str]:
    """Plain-English fighting-style tags from pre-fight stats (needs 3+ UFC fights)."""
    n = _num(r.get(f"{p}_n_fights")) or 0
    if n < 3:
        return []
    tags = []
    checks = [
        ("ko_win_rate", 0.35, "Knockout power"), ("sub_win_rate", 0.25, "Submission threat"),
        ("td_landed_per15", 2.5, "Wrestler"), ("sig_landed_pm", 5.0, "High-volume striker"),
        ("dec_win_rate", 0.5, "Goes the distance"), ("sig_def", 0.62, "Hard to hit"),
    ]
    for col, threshold, label in checks:
        v = _num(r.get(f"{p}_{col}"))
        if v is not None and v >= threshold:
            tags.append(label)
    return tags[:2]


def _profile(r: pd.Series, p: str, side: str) -> dict:
    """Record and tale-of-the-tape fields for one fighter (UFC fights only)."""
    reach, height, age = _num(r.get(f"{p}_reach_cm")), _num(r.get(f"{p}_height_cm")), _num(r.get(f"{p}_age"))
    stance = r.get(f"{p}_stance")
    return {
        f"wins_{side}": _num(r.get(f"{p}_n_wins")), f"losses_{side}": _num(r.get(f"{p}_n_losses")),
        f"streak_{side}": _num(r.get(f"{p}_streak")), f"reach_cm_{side}": reach, f"height_cm_{side}": height,
        f"age_{side}": age, f"stance_{side}": stance if isinstance(stance, str) else None,
        f"styles_{side}": "; ".join(style_tags(r, p)),
    }


@dataclass
class CardFight:
    api_event_id: str
    commence_time: str
    name1: str
    name2: str
    fighter1_id: str
    fighter2_id: str
    known1: bool
    known2: bool
    consensus_p1: float
    best_odds1: float
    best_book1: str
    best_odds2: float
    best_book2: str
    books: int
    captured_at: str


def _fighter_index(conn: sqlite3.Connection) -> tuple[dict[str, list[tuple[str, str]]], list[tuple[str, str]]]:
    """name key -> [(fighter_id, last fight date)], most recent first. Profile-only fighters (debutants) included."""
    last = dict(conn.execute(
        """SELECT fid, MAX(date) FROM (SELECT fighter1_id fid, date FROM fights
                                       UNION ALL SELECT fighter2_id, date FROM fights) GROUP BY fid"""
    ).fetchall())
    idx: dict[str, list[tuple[str, str]]] = defaultdict(list)
    everyone = conn.execute("SELECT fighter_id, name FROM fighters").fetchall()
    for fid, name in everyone:
        for k in name_keys(name):
            idx[k].append((fid, last.get(fid) or ""))
    for k in idx:
        idx[k].sort(key=lambda t: t[1], reverse=True)
    return idx, everyone


def _match(idx: dict[str, list[tuple[str, str]]], everyone: list[tuple[str, str]], name: str) -> str | None:
    for k in name_keys(name):
        if k in idx:
            return idx[k][0][0]  # namesakes: the one who fought most recently
    return fuzzy_unique_match(name, everyone)


def _cluster_cards(times: list[pd.Timestamp], gap_hours: float = 12) -> list[int]:
    """Group fights into cards: a new card starts after a gap of more than ``gap_hours``."""
    order = sorted(range(len(times)), key=lambda i: times[i])
    labels = [0] * len(times)
    card, prev = 0, None
    for i in order:
        if prev is not None and (times[i] - prev).total_seconds() > gap_hours * 3600:
            card += 1
        labels[i] = card
        prev = times[i]
    return labels


def card_from_odds(conn: sqlite3.Connection, max_age_hours: float = 48) -> list[CardFight]:
    snaps = pd.read_sql_query(
        """SELECT * FROM odds_snapshots o WHERE captured_at = (
               SELECT MAX(captured_at) FROM odds_snapshots WHERE api_event_id = o.api_event_id)""", conn)
    if snaps.empty:
        return []
    snaps = snaps[pd.to_datetime(snaps["commence_time"]) > pd.Timestamp.now(tz="UTC")]
    latest = pd.to_datetime(snaps["captured_at"]).max() if not snaps.empty else None
    if latest is not None and (pd.Timestamp.now(tz="UTC") - latest).total_seconds() > max_age_hours * 3600:
        print(f"Warning: newest odds snapshot is from {latest:%Y-%m-%d %H:%M} UTC. "
              "Run: python -m ufc_edge odds")
    idx, everyone = _fighter_index(conn)
    fights = []
    for event_id, ev in snaps.groupby("api_event_id"):
        n1, n2 = ev["home_team"].iloc[0], ev["away_team"].iloc[0]
        f1, f2 = _match(idx, everyone, n1), _match(idx, everyone, n2)
        probs, best = [], {n1: (0.0, ""), n2: (0.0, "")}
        for book, b in ev.groupby("bookmaker_key"):
            prices = dict(zip(b["outcome_name"], b["price"]))
            if n1 in prices and n2 in prices:
                probs.append(devig_power([prices[n1], prices[n2]])[0])
            for n in (n1, n2):
                if n in prices and prices[n] > best[n][0]:
                    best[n] = (float(prices[n]), b["bookmaker_title"].iloc[0])
        if not probs or best[n1][0] <= 1 or best[n2][0] <= 1:
            continue
        fights.append(CardFight(
            api_event_id=event_id, commence_time=ev["commence_time"].iloc[0], name1=n1, name2=n2,
            fighter1_id=f1 or f"new:{normalise_name(n1)}", fighter2_id=f2 or f"new:{normalise_name(n2)}",
            known1=f1 is not None, known2=f2 is not None, consensus_p1=float(np.median(probs)),
            best_odds1=best[n1][0], best_book1=best[n1][1], best_odds2=best[n2][0], best_book2=best[n2][1],
            books=len(probs), captured_at=ev["captured_at"].iloc[0],
        ))
    # The feed covers all MMA. Keep a card only if most of its fights are between known UFC names.
    if fights:
        labels = _cluster_cards([pd.Timestamp(f.commence_time) for f in fights])
        keep = []
        for card in set(labels):
            members = [f for f, lab in zip(fights, labels) if lab == card]
            known_share = sum(f.known1 and f.known2 for f in members) / len(members)
            if known_share >= 0.5:
                keep.extend(members)
            else:
                print(f"Skipping {len(members)} fight(s) starting {members[0].commence_time[:16]}: "
                      "mostly fighters not on UFC records, so probably another promotion.")
        fights = keep
    return sorted(fights, key=lambda f: f.commence_time)


def predict_card(conn: sqlite3.Connection, bundle: ModelBundle, cfg: RiskConfig | None = None) -> pd.DataFrame:
    cfg = cfg or RiskConfig()
    lo, hi = float(cfg.min_decimal_odds), float(cfg.max_decimal_odds)
    card = card_from_odds(conn)
    if not card:
        return pd.DataFrame()
    with conn:
        conn.execute("DELETE FROM upcoming_bouts")
        for i, f in enumerate(card):
            conn.execute(
                """INSERT INTO upcoming_bouts(event_id, event_name, date, bout_order, fight_id, fighter1_id,
                     fighter1_name, fighter2_id, fighter2_name, weight_class, scraped_at)
                   VALUES ('odds', 'From odds feed', ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (f.commence_time[:10], i, f.api_event_id, f.fighter1_id, f.name1, f.fighter2_id, f.name2,
                 f.captured_at))
    feats = attach_market_odds(build_fight_features(conn), conn)
    up = feats[feats["is_upcoming"] == 1].copy()
    up["bout_order"] = up["fight_id"].str.rsplit(":", n=1).str[-1].astype(int)
    up = up.set_index("bout_order").sort_index()

    p_stats_a = bundle.stats.predict(up)
    methods_a = bundle.methods.predict(up)
    factors_a = bundle.explainer.top_factors(up)
    rows = []
    for (order, r), ps_a, (_, m), fac in zip(up.iterrows(), p_stats_a, methods_a.iterrows(), factors_a):
        f = card[order]
        a_is_1 = r["a_id"] == f.fighter1_id
        p_stats1 = ps_a if a_is_1 else 1 - ps_a
        p_final1 = float(bundle.blend.predict(np.array([f.consensus_p1]), np.array([p_stats1]))[0])
        side = (lambda s: s) if a_is_1 else (lambda s: {"A": "B", "B": "A"}[s])
        meth = {f"{who}_{k}": float(m[f"{side(lbl)}_{k}"])
                for who, lbl in (("f1", "A"), ("f2", "B")) for k in ("KO_TKO", "SUB", "DEC")}
        ev1 = expected_value(p_final1, f.best_odds1)
        ev2 = expected_value(1 - p_final1, f.best_odds2)
        def fav(v: float) -> str:  # favours fighter 1 if positive contribution towards fighter 1
            return f.name1 if (v > 0) == a_is_1 else f.name2
        warnings = []
        for known, name, nf in ((f.known1, f.name1, r["a_n_fights"] if a_is_1 else r["b_n_fights"]),
                                (f.known2, f.name2, r["b_n_fights"] if a_is_1 else r["a_n_fights"])):
            if not known:
                warnings.append(f"{name}: not found in UFC records (name mismatch or not a UFC fighter)")
            elif nf is not None and nf == 0:
                warnings.append(f"{name}: UFC debut (no UFC fight stats)")
            elif nf is not None and nf < 3:
                warnings.append(f"{name}: only {int(nf)} previous UFC fight{'s' if nf != 1 else ''}")
        if f.books < 3:
            warnings.append(f"only {f.books} bookmakers pricing this fight")
        rows.append({
            "date": f.commence_time[:16].replace("T", " "),
            "commence_time": f.commence_time, "api_event_id": f.api_event_id,
            "fighter_1": f.name1, "fighter_2": f.name2,
            "fighter_1_id": f.fighter1_id, "fighter_2_id": f.fighter2_id,
            "n_fights_1": float(r["a_n_fights"] if a_is_1 else r["b_n_fights"]),
            "n_fights_2": float(r["b_n_fights"] if a_is_1 else r["a_n_fights"]),
            **_profile(r, "a" if a_is_1 else "b", "1"),
            **_profile(r, "b" if a_is_1 else "a", "2"),
            "p_stats_1": p_stats1, "p_market_1": f.consensus_p1, "p_model_1": p_final1,
            "best_odds_1": f.best_odds1, "best_book_1": f.best_book1,
            "best_odds_2": f.best_odds2, "best_book_2": f.best_book2,
            "ev_1": ev1, "ev_2": ev2,
            "value_flag": (f.name1 if ev1 >= cfg.min_edge and lo <= f.best_odds1 <= hi
                           else f.name2 if ev2 >= cfg.min_edge and lo <= f.best_odds2 <= hi else ""),
            **meth,
            "factors": "; ".join(f"{FACTOR_LABELS.get(n, n)} → {fav(v)}" for n, v in fac),
            "warnings": "; ".join(warnings),
            "bookmakers": f.books, "odds_captured_at": f.captured_at,
        })
    return pd.DataFrame(rows)
