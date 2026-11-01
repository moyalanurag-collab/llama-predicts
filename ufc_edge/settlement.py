"""Settle open bets against verified UFC results, and record closing-line value.

Results come from the UFCStats history (``update-data``). A bet settles only
when its fight is found in that history:

* the same two fighters (by id, or by name for fighters without one yet),
* on a date within two days of the scheduled start.

Draws and no contests settle as VOID (stake returned), the usual rule for
two-way fight-winner markets. A fight that hasn't appeared yet stays open.
Nothing is guessed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from .data.names import same_fighter
from .ledger import BankrollService

RESULT_SOURCE = "UFCStats results (scrape_ufc_stats export)"


@dataclass
class SettleReport:
    settled: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _find_result(stats: sqlite3.Connection, ctx: sqlite3.Row) -> tuple[str, str | None] | None:
    """(outcome, winner_id) for the fight, or None if it isn't in the results yet."""
    start = pd.Timestamp(ctx["commence_time"])
    lo, hi = (start - timedelta(days=2)).strftime("%Y-%m-%d"), (start + timedelta(days=2)).strftime("%Y-%m-%d")
    cur = stats.cursor()
    cur.row_factory = sqlite3.Row
    rows = cur.execute(
        """SELECT f.fight_id, f.fighter1_id, f.fighter2_id, f.outcome, f.winner_id,
                  a.name AS n1, b.name AS n2
           FROM fights f LEFT JOIN fighters a ON a.fighter_id = f.fighter1_id
                         LEFT JOIN fighters b ON b.fighter_id = f.fighter2_id
           WHERE f.date BETWEEN ? AND ?""", (lo, hi)).fetchall()
    me, opp = ctx["fighter_id"], ctx["opponent_id"]
    for r in rows:
        ids = {r["fighter1_id"], r["fighter2_id"]}
        by_id = me in ids and opp in ids
        by_name = ({same_fighter(ctx["fighter_name"], r["n1"] or ""), same_fighter(ctx["fighter_name"], r["n2"] or "")}
                   == {True, False}) and (same_fighter(ctx["opponent_name"], r["n1"] or "")
                                          or same_fighter(ctx["opponent_name"], r["n2"] or ""))
        if by_id or by_name:
            if r["outcome"] != "WIN":
                return r["outcome"], None
            mine = r["fighter1_id"] if (r["fighter1_id"] == me or same_fighter(ctx["fighter_name"], r["n1"] or "")) \
                else r["fighter2_id"]
            return "WIN", ("me" if r["winner_id"] == mine else "opponent")
    return None


def closing_odds(stats: sqlite3.Connection, api_event_id: str, selection: str, commence_time: str) -> float | None:
    """Best UK price in the last snapshot taken before the fight started."""
    try:
        row = stats.execute(
            """SELECT MAX(price) FROM odds_snapshots
               WHERE api_event_id = ? AND outcome_name = ? AND captured_at = (
                   SELECT MAX(captured_at) FROM odds_snapshots WHERE api_event_id = ? AND captured_at < ?)""",
            (api_event_id, selection, api_event_id, commence_time.replace("+00:00", "Z"))).fetchone()
    except sqlite3.OperationalError:
        return None
    return float(row[0]) if row and row[0] else None


def settle_open_bets(svc: BankrollService, stats: sqlite3.Connection) -> SettleReport:
    report = SettleReport()
    open_bets = svc.conn.execute(
        """SELECT b.id, b.selection, b.decimal_odds, c.* FROM bets b JOIN bet_context c ON c.bet_id = b.id
           WHERE b.status = 'OPEN'""").fetchall()
    now = svc.clock()
    for ctx in open_bets:
        label = f"{ctx['fighter_name']} vs {ctx['opponent_name']}"
        if pd.Timestamp(ctx["commence_time"]) > pd.Timestamp(now):
            report.waiting.append(f"{label}: not started yet")
            continue
        result = _find_result(stats, ctx)
        if result is None:
            report.waiting.append(f"{label}: result not in the data yet (run update-data after the event)")
            continue
        outcome, who = result
        verdict = "VOID" if outcome != "WIN" else ("WON" if who == "me" else "LOST")
        close = closing_odds(stats, ctx["api_event_id"], ctx["selection"], ctx["commence_time"])
        try:
            svc.settle_bet(ctx["id"], verdict, RESULT_SOURCE, verified=True, actor="system")
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
            report.problems.append(f"{label}: {exc}")
            continue
        with svc.conn:
            svc.conn.execute("UPDATE bet_context SET closing_odds = ?, clv = ? WHERE bet_id = ?",
                             (close, (float(ctx["decimal_odds"]) / close - 1) if close else None, ctx["id"]))
        report.settled.append(f"{label}: {verdict}")
    return report
