"""Point-in-time fight features.

For each fight, every feature for each fighter is computed **only from that
fighter's earlier fights**: the fight itself and anything after it are
excluded. That's what keeps the backtest honest. The test suite checks it
directly: building features on the full history, and again on history cut off
at a date, must give identical rows for every fight up to that date.

Other safeguards
----------------
* **Corner order.** UFCStats lists the winner first. Each fight is
  re-oriented into fighter A and fighter B using a hash of the fight id, so A
  wins about half the time and the order carries no information.
* **Current-snapshot fields.** UFCStats career averages (SLpM and so on) and
  current records are *never* used, because they include future fights. Only
  static attributes are taken from the fighter profile: height, reach, stance
  and date of birth.
* **Not modelled.** Short-notice replacements, missed weight and injuries
  have no reliable timestamped source (see docs/DATA_SOURCES.md).

The output is one row per fight with ``a_*``, ``b_*`` and ``diff_*`` (A - B)
columns, plus the label ``a_wins``. Draws and no contests have a missing
label. Upcoming bouts are included with ``is_upcoming = 1``.
"""

from __future__ import annotations

import hashlib
import sqlite3

import numpy as np
import pandas as pd

ELO_START = 1500.0
ELO_K = 32.0
RECENT_N = 5
ROUND_SEC = 300

# Per-fight quantities summed over a fighter's past fights.
_OWN = ["sig_landed", "sig_att", "td_landed", "td_att", "sub_att", "kd", "ctrl_sec",
        "head_landed", "distance_landed", "clinch_landed", "ground_landed"]
_OPP = ["sig_landed", "sig_att", "td_landed", "td_att", "kd", "ctrl_sec"]

# Fighter-level feature names (each appears as a_*, b_* and diff_*).
NUMERIC_FEATURES = [
    "elo", "n_fights", "n_wins", "n_losses", "win_rate", "win_rate_last5", "streak",
    "days_since_last", "age", "height_cm", "reach_cm",
    "sig_landed_pm", "sig_absorbed_pm", "sig_acc", "sig_def", "sig_landed_pm_last5",
    "sig_absorbed_pm_last5", "td_landed_per15", "td_acc", "td_def", "sub_att_per15",
    "kd_per15", "kd_absorbed_per15", "ctrl_share", "ctrl_against_share",
    "head_share", "distance_share", "clinch_share", "ground_share",
    "ko_win_rate", "sub_win_rate", "dec_win_rate", "ko_loss_rate", "sub_loss_rate",
    "finish_rate", "avg_fight_min", "five_round_exp", "title_fight_exp",
    "late_output_ratio", "opp_elo_avg", "minutes_with_stats",
]


# ---------------------------------------------------------------- loading
def load_tables(conn: sqlite3.Connection, include_upcoming: bool = True) -> tuple[pd.DataFrame, ...]:
    fights = pd.read_sql_query("SELECT * FROM fights", conn)
    stats = pd.read_sql_query("SELECT * FROM fight_stats", conn)
    fighters = pd.read_sql_query("SELECT * FROM fighters", conn)
    if include_upcoming:
        try:
            up = pd.read_sql_query(
                "SELECT * FROM upcoming_bouts WHERE fighter1_id IS NOT NULL AND fighter2_id IS NOT NULL", conn)
        except Exception:  # table may not exist yet
            up = pd.DataFrame()
        if not up.empty:
            up = pd.DataFrame({
                "fight_id": "upcoming:" + up["event_id"] + ":" + up["bout_order"].astype(str),
                "event_id": up["event_id"], "date": up["date"], "bout_order": up["bout_order"],
                "fighter1_id": up["fighter1_id"], "fighter2_id": up["fighter2_id"],
                "outcome": "PENDING", "weight_class": up["weight_class"],
            })
            fights = pd.concat([fights, up], ignore_index=True)
    return fights, stats, fighters


# ---------------------------------------------------------------- helpers
def _orient_swap(fight_id: str) -> bool:
    """Deterministic coin flip per fight: True means fighter2 becomes A."""
    return int(hashlib.md5(fight_id.encode()).hexdigest(), 16) % 2 == 1


def _ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    return num / den.where(den > 0)


def _prior_sum(df: pd.DataFrame, col: str) -> pd.Series:
    """Sum of ``col`` over each fighter's *earlier* appearances."""
    vals = df[col].fillna(0)
    return vals.groupby(df["fighter_id"]).cumsum() - vals


def _prior_rolling(df: pd.DataFrame, col: str, n: int) -> pd.Series:
    vals = df[col].fillna(0)
    return vals.groupby(df["fighter_id"]).transform(lambda s: s.shift(1).rolling(n, min_periods=1).sum())


# ---------------------------------------------------------------- elo
def compute_elo(fights: pd.DataFrame) -> dict[tuple[str, str], float]:
    """Pre-fight Elo for every (fight_id, fighter_id). Ratings update only after a result."""
    ratings: dict[str, float] = {}
    pre: dict[tuple[str, str], float] = {}
    for f in fights.itertuples(index=False):
        a, b = f.fighter1_id, f.fighter2_id
        ra, rb = ratings.get(a, ELO_START), ratings.get(b, ELO_START)
        pre[(f.fight_id, a)], pre[(f.fight_id, b)] = ra, rb
        if f.outcome == "WIN":
            sa = 1.0 if f.winner_id == a else 0.0
        elif f.outcome == "DRAW":
            sa = 0.5
        else:
            continue  # no contest or pending: no update
        ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400))
        ratings[a] = ra + ELO_K * (sa - ea)
        ratings[b] = rb + ELO_K * ((1 - sa) - (1 - ea))
    return pre


# ---------------------------------------------------------------- per-round cardio
def _round_splits(fights: pd.DataFrame, stats: pd.DataFrame) -> pd.DataFrame:
    """Per fighter per fight: significant strikes and minutes in rounds 1-2 vs 3+."""
    rs = stats[stats["round"] > 0][["fight_id", "fighter_id", "round", "sig_landed"]]
    rs = rs.merge(fights[["fight_id", "end_round", "end_time_sec"]], on="fight_id", how="left")
    rs = rs[rs["end_round"].notna() & rs["end_time_sec"].notna()]
    rs["sec"] = np.where(rs["round"] < rs["end_round"], ROUND_SEC,
                         np.where(rs["round"] == rs["end_round"], rs["end_time_sec"], 0))
    late = rs["round"] >= 3
    out = pd.DataFrame({
        "fight_id": rs["fight_id"], "fighter_id": rs["fighter_id"],
        "early_sig": np.where(~late, rs["sig_landed"].fillna(0), 0),
        "early_sec": np.where(~late, rs["sec"], 0),
        "late_sig": np.where(late, rs["sig_landed"].fillna(0), 0),
        "late_sec": np.where(late, rs["sec"], 0),
    })
    return out.groupby(["fight_id", "fighter_id"], as_index=False).sum()


# ---------------------------------------------------------------- appearances
def build_appearances(fights: pd.DataFrame, stats: pd.DataFrame, fighters: pd.DataFrame) -> pd.DataFrame:
    """One row per fighter per fight with pre-fight features attached."""
    fights = fights.copy()
    fights["date"] = pd.to_datetime(fights["date"])
    # Chronological order. Within one night, higher bout_order = earlier on the card.
    fights = fights.sort_values(["date", "bout_order"], ascending=[True, False], kind="mergesort")
    fights = fights.reset_index(drop=True)
    fights["seq"] = np.arange(len(fights))
    elo = compute_elo(fights)

    s0 = stats[stats["round"] == 0]
    rows = []
    for side, other in ((1, 2), (2, 1)):
        part = fights[["fight_id", "seq", "date", "outcome", "winner_id", "method_group",
                       "fight_duration_sec", "scheduled_rounds", "title_bout"]].copy()
        part["fighter_id"] = fights[f"fighter{side}_id"]
        part["opponent_id"] = fights[f"fighter{other}_id"]
        rows.append(part)
    app = pd.concat(rows, ignore_index=True)

    own = s0.rename(columns={c: f"own_{c}" for c in _OWN})[["fight_id", "fighter_id"] + [f"own_{c}" for c in _OWN]]
    opp = s0.rename(columns={"fighter_id": "opponent_id", **{c: f"opp_{c}" for c in _OPP}})
    opp = opp[["fight_id", "opponent_id"] + [f"opp_{c}" for c in _OPP]]
    app = app.merge(own, on=["fight_id", "fighter_id"], how="left")
    app = app.merge(opp, on=["fight_id", "opponent_id"], how="left")
    app = app.merge(_round_splits(fights, stats), on=["fight_id", "fighter_id"], how="left")
    app = app.sort_values(["seq", "fighter_id"], kind="mergesort").reset_index(drop=True)

    app["elo"] = [elo[(f, i)] for f, i in zip(app["fight_id"], app["fighter_id"])]
    app["opp_elo_at_fight"] = [elo[(f, o)] for f, o in zip(app["fight_id"], app["opponent_id"])]

    # Per-appearance outcome flags (zero for pending fights and no contests).
    done = app["outcome"].isin(["WIN", "DRAW"])
    won = (app["outcome"] == "WIN") & (app["winner_id"] == app["fighter_id"])
    lost = (app["outcome"] == "WIN") & (app["winner_id"] != app["fighter_id"])
    mg = app["method_group"]
    app["_n"] = done.astype(int)
    app["_win"] = won.astype(int)
    app["_loss"] = lost.astype(int)
    app["_ko_win"] = (won & (mg == "KO_TKO")).astype(int)
    app["_sub_win"] = (won & (mg == "SUB")).astype(int)
    app["_dec_win"] = (won & (mg == "DEC")).astype(int)
    app["_ko_loss"] = (lost & (mg == "KO_TKO")).astype(int)
    app["_sub_loss"] = (lost & (mg == "SUB")).astype(int)
    app["_five"] = (done & (app["scheduled_rounds"] == 5)).astype(int)
    app["_title"] = (done & (app["title_bout"] == 1)).astype(int)
    app["_min"] = np.where(done, app["fight_duration_sec"].fillna(0) / 60, 0)
    has_stats = done & app["own_sig_att"].notna() & app["fight_duration_sec"].notna()
    app["_smin"] = np.where(has_stats, app["fight_duration_sec"] / 60, 0)
    app["_opp_elo"] = np.where(done, app["opp_elo_at_fight"], 0)
    for c in [f"own_{c}" for c in _OWN] + [f"opp_{c}" for c in _OPP] + \
             ["early_sig", "early_sec", "late_sig", "late_sec"]:
        app[c] = np.where(has_stats, app[c].fillna(0), 0)

    P = {c: _prior_sum(app, c) for c in
         ["_n", "_win", "_loss", "_ko_win", "_sub_win", "_dec_win", "_ko_loss", "_sub_loss",
          "_five", "_title", "_min", "_smin", "_opp_elo"]
         + [f"own_{c}" for c in _OWN] + [f"opp_{c}" for c in _OPP]
         + ["early_sig", "early_sec", "late_sig", "late_sec"]}
    R = {c: _prior_rolling(app, c, RECENT_N) for c in
         ["_n", "_win", "_smin", "own_sig_landed", "opp_sig_landed"]}

    n, smin = P["_n"], P["_smin"]
    f = pd.DataFrame({"fight_id": app["fight_id"], "fighter_id": app["fighter_id"], "seq": app["seq"]})
    f["elo"] = app["elo"]
    f["n_fights"] = n
    f["n_wins"] = P["_win"]
    f["n_losses"] = P["_loss"]
    f["win_rate"] = (P["_win"] + 1) / (n + 2)  # Laplace prior so debuts sit at 0.5
    f["win_rate_last5"] = _ratio(R["_win"], R["_n"])
    f["sig_landed_pm"] = _ratio(P["own_sig_landed"], smin)
    f["sig_absorbed_pm"] = _ratio(P["opp_sig_landed"], smin)
    f["sig_acc"] = _ratio(P["own_sig_landed"], P["own_sig_att"])
    f["sig_def"] = 1 - _ratio(P["opp_sig_landed"], P["opp_sig_att"])
    f["sig_landed_pm_last5"] = _ratio(R["own_sig_landed"], R["_smin"])
    f["sig_absorbed_pm_last5"] = _ratio(R["opp_sig_landed"], R["_smin"])
    f["td_landed_per15"] = _ratio(P["own_td_landed"] * 15, smin)
    f["td_acc"] = _ratio(P["own_td_landed"], P["own_td_att"])
    f["td_def"] = 1 - _ratio(P["opp_td_landed"], P["opp_td_att"])
    f["sub_att_per15"] = _ratio(P["own_sub_att"] * 15, smin)
    f["kd_per15"] = _ratio(P["own_kd"] * 15, smin)
    f["kd_absorbed_per15"] = _ratio(P["opp_kd"] * 15, smin)
    f["ctrl_share"] = _ratio(P["own_ctrl_sec"] / 60, smin)
    f["ctrl_against_share"] = _ratio(P["opp_ctrl_sec"] / 60, smin)
    for zone in ("head", "distance", "clinch", "ground"):
        f[f"{zone}_share"] = _ratio(P[f"own_{zone}_landed"], P["own_sig_landed"])
    f["ko_win_rate"] = _ratio(P["_ko_win"], n)
    f["sub_win_rate"] = _ratio(P["_sub_win"], n)
    f["dec_win_rate"] = _ratio(P["_dec_win"], n)
    f["ko_loss_rate"] = _ratio(P["_ko_loss"], n)
    f["sub_loss_rate"] = _ratio(P["_sub_loss"], n)
    f["finish_rate"] = _ratio(P["_ko_win"] + P["_sub_win"], P["_win"])
    f["avg_fight_min"] = _ratio(P["_min"], n)
    f["five_round_exp"] = P["_five"]
    f["title_fight_exp"] = P["_title"]
    early_pm = _ratio(P["early_sig"], P["early_sec"] / 60)
    late_pm = _ratio(P["late_sig"], P["late_sec"] / 60)
    f["late_output_ratio"] = _ratio(late_pm, early_pm).where(P["late_sec"] >= 300)
    f["opp_elo_avg"] = _ratio(P["_opp_elo"], n)
    f["minutes_with_stats"] = smin

    # Layoff, age and streak.
    prev_date = app.groupby("fighter_id")["date"].shift(1)
    f["days_since_last"] = (app["date"] - prev_date).dt.days
    f["streak"] = _streaks(app)
    prof = fighters.set_index("fighter_id")
    dob = pd.to_datetime(app["fighter_id"].map(prof["dob"]) if "dob" in prof else None, errors="coerce")
    f["age"] = (app["date"] - dob).dt.days / 365.25
    f["height_cm"] = app["fighter_id"].map(prof["height_cm"]) if "height_cm" in prof else np.nan
    f["reach_cm"] = app["fighter_id"].map(prof["reach_cm"]) if "reach_cm" in prof else np.nan
    f["stance"] = app["fighter_id"].map(prof["stance"]) if "stance" in prof else None
    return f


def _streaks(app: pd.DataFrame) -> pd.Series:
    """Pre-fight streak: +3 = three straight wins, -2 = two straight losses."""
    out = np.zeros(len(app))
    current: dict[str, int] = {}
    for i, (fid, outcome, winner) in enumerate(zip(app["fighter_id"], app["outcome"], app["winner_id"])):
        s = current.get(fid, 0)
        out[i] = s
        if outcome == "WIN":
            if winner == fid:
                current[fid] = s + 1 if s > 0 else 1
            else:
                current[fid] = s - 1 if s < 0 else -1
        elif outcome == "DRAW":
            current[fid] = 0
    return pd.Series(out, index=app.index)


# ---------------------------------------------------------------- fight rows
def build_fight_features(conn: sqlite3.Connection, include_upcoming: bool = True) -> pd.DataFrame:
    fights, stats, fighters = load_tables(conn, include_upcoming)
    return assemble(fights, stats, fighters)


def assemble(fights: pd.DataFrame, stats: pd.DataFrame, fighters: pd.DataFrame) -> pd.DataFrame:
    if fights.empty:
        return pd.DataFrame()
    feats = build_appearances(fights, stats, fighters).set_index(["fight_id", "fighter_id"])
    names = fighters.set_index("fighter_id")["name"] if not fighters.empty else pd.Series(dtype=str)

    base = fights.copy()
    swap = base["fight_id"].map(_orient_swap)
    base["a_id"] = np.where(swap, base["fighter2_id"], base["fighter1_id"])
    base["b_id"] = np.where(swap, base["fighter1_id"], base["fighter2_id"])
    out = pd.DataFrame({
        "fight_id": base["fight_id"],
        "event_id": base["event_id"],
        "date": pd.to_datetime(base["date"]),
        "is_upcoming": (base["outcome"] == "PENDING").astype(int),
        "weight_class": base.get("weight_class"),
        "women": base.get("women"),
        "title_bout": base.get("title_bout"),
        "scheduled_rounds": base.get("scheduled_rounds"),
        "a_id": base["a_id"], "b_id": base["b_id"],
        "a_name": base["a_id"].map(names), "b_name": base["b_id"].map(names),
    })
    label = np.where(base["outcome"] == "WIN", (base["winner_id"] == base["a_id"]).astype(float), np.nan)
    out["a_wins"] = label
    out["method_group"] = base.get("method_group")

    a = feats.reindex(list(zip(out["fight_id"], out["a_id"]))).reset_index(drop=True)
    b = feats.reindex(list(zip(out["fight_id"], out["b_id"]))).reset_index(drop=True)
    out = out.reset_index(drop=True)
    cols = {}
    for c in NUMERIC_FEATURES:
        cols[f"a_{c}"] = a[c].astype(float)
        cols[f"b_{c}"] = b[c].astype(float)
        cols[f"diff_{c}"] = a[c].astype(float) - b[c].astype(float)
    cols["a_stance"], cols["b_stance"] = a["stance"], b["stance"]
    cols["seq"] = a["seq"]
    out = pd.concat([out, pd.DataFrame(cols)], axis=1)
    return out.sort_values("seq", kind="mergesort").drop(columns="seq").reset_index(drop=True)


# ---------------------------------------------------------------- market odds
def attach_market_odds(df: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
    """Add each side's historical decimal odds and the de-vigged market probability for A.

    These columns are for benchmarking and backtesting (the model has to beat
    them) and for computing betting value. They are approximate closing odds
    with no timestamp, so they must never be treated as known before fight week.
    """
    from ..ev import devig_power

    try:
        odds = pd.read_sql_query("SELECT fight_id, fighter_id, decimal_odds FROM historical_odds", conn)
    except Exception:  # table not loaded yet
        return df
    if odds.empty or df.empty:
        return df
    lookup = odds.set_index(["fight_id", "fighter_id"])["decimal_odds"]
    out = df.copy()
    out["a_odds"] = lookup.reindex(list(zip(out["fight_id"], out["a_id"]))).to_numpy()
    out["b_odds"] = lookup.reindex(list(zip(out["fight_id"], out["b_id"]))).to_numpy()
    both = out["a_odds"].notna() & out["b_odds"].notna()
    out["market_p_a"] = np.nan
    out.loc[both, "market_p_a"] = [devig_power([a, b])[0] for a, b in zip(out.loc[both, "a_odds"],
                                                                          out.loc[both, "b_odds"])]
    out["overround"] = 1 / out["a_odds"] + 1 / out["b_odds"] - 1
    return out
