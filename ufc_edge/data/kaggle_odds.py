"""Historical odds from the Kaggle "Ultimate UFC Dataset" (ufc-master.csv).

The dataset gives one American moneyline per fighter per fight (plus
method-of-victory props for most fights since 2012). There's **no timestamp
and no bookmaker**, so these prices are treated as approximate closing odds.
That's good enough to benchmark the model against the market and to
backtest. It isn't good enough to study line movement.

Only the odds are used from this file. Its other columns (career averages,
streaks, rankings) are not point-in-time safe, so they are ignored.

Each Kaggle row is matched to a fight in ``data/ufc.db`` by date (±1 day)
and both fighters' names, in either corner order.
"""

from __future__ import annotations

import csv
import difflib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..ev import american_to_decimal
from .names import normalise_name, same_fighter

SOURCE = "kaggle_ultimate_ufc"

SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_odds (
    fight_id      TEXT NOT NULL,
    fighter_id    TEXT NOT NULL,
    source        TEXT NOT NULL,
    american_odds REAL NOT NULL,
    decimal_odds  REAL NOT NULL CHECK (decimal_odds > 1),
    dec_odds      REAL,   -- American odds to win by decision
    sub_odds      REAL,   -- ... by submission
    ko_odds       REAL,   -- ... by KO/TKO
    PRIMARY KEY (fight_id, fighter_id, source)
);
"""


def _loosely_same(a: str, b: str) -> bool:
    """Weaker match for name changes (marriage, ring names, spelling):
    shared first or last name, or a very similar spelling."""
    pa, pb = normalise_name(a).split(), normalise_name(b).split()
    if not pa or not pb:
        return False
    if pa[0] == pb[0] or pa[-1] == pb[-1]:
        return True
    return difflib.SequenceMatcher(None, " ".join(pa), " ".join(pb)).ratio() >= 0.75


@dataclass
class OddsLoadReport:
    rows: int = 0
    matched: int = 0
    matched_loosely: list[str] = field(default_factory=list)
    no_odds: int = 0
    unmatched: list[str] = field(default_factory=list)
    first_date: str | None = None
    last_date: str | None = None


def _num(value: str) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v <= -100 or v >= 100 else None


def load_kaggle_odds(conn: sqlite3.Connection, csv_path: str | Path) -> OddsLoadReport:
    conn.executescript(SCHEMA)
    report = OddsLoadReport()

    names = dict(conn.execute("SELECT fighter_id, name FROM fighters").fetchall())
    by_date: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for fight_id, d, f1, f2 in conn.execute("SELECT fight_id, date, fighter1_id, fighter2_id FROM fights"):
        if d:
            by_date[d].append((fight_id, f1, f2))

    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    report.rows = len(rows)
    dates = sorted(r["date"] for r in rows if r.get("date"))
    report.first_date, report.last_date = (dates[0], dates[-1]) if dates else (None, None)

    out: list[tuple] = []
    for r in rows:
        red, blue = r.get("R_fighter", "").strip(), r.get("B_fighter", "").strip()
        r_odds, b_odds = _num(r.get("R_odds", "")), _num(r.get("B_odds", ""))
        if r_odds is None or b_odds is None:
            report.no_odds += 1
            continue
        try:
            d = date.fromisoformat(r["date"][:10])
        except (KeyError, ValueError):
            report.unmatched.append(f"{r.get('date')} {red} vs {blue}: bad date")
            continue
        match = None
        for delta in (0, -1, 1):
            for fight_id, f1, f2 in by_date.get((d + timedelta(days=delta)).isoformat(), []):
                n1, n2 = names.get(f1, ""), names.get(f2, "")
                if same_fighter(red, n1) and same_fighter(blue, n2):
                    match = (fight_id, f1, f2)
                elif same_fighter(red, n2) and same_fighter(blue, n1):
                    match = (fight_id, f2, f1)
                if match:
                    break
            if match:
                break
        if match is None:
            # Fallback: one fighter matches exactly, the other loosely, and only one fight fits.
            loose = []
            for delta in (0, -1, 1):
                for fight_id, f1, f2 in by_date.get((d + timedelta(days=delta)).isoformat(), []):
                    n1, n2 = names.get(f1, ""), names.get(f2, "")
                    if (same_fighter(red, n1) and _loosely_same(blue, n2)) or \
                       (same_fighter(blue, n2) and _loosely_same(red, n1)):
                        loose.append((fight_id, f1, f2, n1, n2))
                    elif (same_fighter(red, n2) and _loosely_same(blue, n1)) or \
                         (same_fighter(blue, n1) and _loosely_same(red, n2)):
                        loose.append((fight_id, f2, f1, n2, n1))
                if loose:
                    break
            if len(loose) == 1:
                fight_id, rid, bid, rn, bn = loose[0]
                match = (fight_id, rid, bid)
                report.matched_loosely.append(f"{d} {red} vs {blue} = {rn} vs {bn}")
        if match is None:
            report.unmatched.append(f"{d} {red} vs {blue}")
            continue
        fight_id, red_id, blue_id = match
        for fid, odds, p in ((red_id, r_odds, "r"), (blue_id, b_odds, "b")):
            out.append((fight_id, fid, SOURCE, odds, round(american_to_decimal(odds), 4),
                        _num(r.get(f"{p}_dec_odds", "")), _num(r.get(f"{p}_sub_odds", "")),
                        _num(r.get(f"{p}_ko_odds", ""))))
        report.matched += 1

    with conn:
        conn.execute("DELETE FROM historical_odds WHERE source = ?", (SOURCE,))
        conn.executemany("INSERT OR REPLACE INTO historical_odds VALUES (?,?,?,?,?,?,?,?)", out)
    return report
