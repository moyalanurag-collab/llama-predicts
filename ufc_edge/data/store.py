"""Local fight-statistics database (``data/ufc.db``).

This is kept separate from the betting ledgers (paper.db and live.db).
Scraped data can be rebuilt at any time. The ledger can't.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from .ufcstats_parse import EventPage, FighterPage, FightPage, StatLine

STAT_COLUMNS = [f.name for f in fields(StatLine)]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    date        TEXT,              -- YYYY-MM-DD
    location    TEXT,
    is_complete INTEGER NOT NULL DEFAULT 0,
    scraped_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fighters (
    fighter_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    height_cm  REAL,
    reach_cm   REAL,
    stance     TEXT,
    dob        TEXT,
    scraped_at TEXT NOT NULL
);

-- fighter1/fighter2 follow the site's order, which lists the WINNER FIRST.
-- Never use that order as a model input.
CREATE TABLE IF NOT EXISTS fights (
    fight_id           TEXT PRIMARY KEY,
    event_id           TEXT NOT NULL REFERENCES events(event_id),
    date               TEXT,
    bout_order         INTEGER,
    fighter1_id        TEXT NOT NULL,
    fighter2_id        TEXT NOT NULL,
    fighter1_status    TEXT,
    fighter2_status    TEXT,
    winner_id          TEXT,
    outcome            TEXT NOT NULL CHECK (outcome IN ('WIN','DRAW','NC')),
    bout_type          TEXT,
    weight_class       TEXT,
    title_bout         INTEGER,
    women              INTEGER,
    method             TEXT,
    method_group       TEXT,
    method_detail      TEXT,
    end_round          INTEGER,
    end_time_sec       INTEGER,
    time_format        TEXT,
    scheduled_rounds   INTEGER,
    fight_duration_sec INTEGER,
    referee            TEXT
);
CREATE INDEX IF NOT EXISTS ix_fights_date ON fights(date);
CREATE INDEX IF NOT EXISTS ix_fights_f1 ON fights(fighter1_id);
CREATE INDEX IF NOT EXISTS ix_fights_f2 ON fights(fighter2_id);

-- round = 0 is the whole fight; 1..5 are individual rounds.
CREATE TABLE IF NOT EXISTS fight_stats (
    fight_id   TEXT NOT NULL REFERENCES fights(fight_id),
    fighter_id TEXT NOT NULL,
    round      INTEGER NOT NULL,
    {", ".join(f"{c} INTEGER" for c in STAT_COLUMNS)},
    PRIMARY KEY (fight_id, fighter_id, round)
);

-- Scheduled bouts on upcoming cards; replaced on every refresh.
CREATE TABLE IF NOT EXISTS upcoming_bouts (
    event_id      TEXT NOT NULL,
    event_name    TEXT NOT NULL,
    date          TEXT,
    bout_order    INTEGER NOT NULL,
    fight_id      TEXT,
    fighter1_id   TEXT,
    fighter1_name TEXT NOT NULL,
    fighter2_id   TEXT,
    fighter2_name TEXT NOT NULL,
    weight_class  TEXT,
    scraped_at    TEXT NOT NULL,
    PRIMARY KEY (event_id, bout_order)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StatsStore:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def event_is_complete(self, event_id: str) -> bool:
        row = self.conn.execute("SELECT is_complete FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return bool(row and row["is_complete"])

    def has_fighter(self, fighter_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM fighters WHERE fighter_id = ?", (fighter_id,)
        ).fetchone() is not None

    def save_fighter(self, f: FighterPage) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO fighters(fighter_id, name, height_cm, reach_cm, stance, dob, scraped_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(fighter_id) DO UPDATE SET name=excluded.name,
                     height_cm=excluded.height_cm, reach_cm=excluded.reach_cm,
                     stance=excluded.stance, dob=excluded.dob, scraped_at=excluded.scraped_at""",
                (f.fighter_id, f.name, f.height_cm, f.reach_cm, f.stance,
                 f.dob.isoformat() if f.dob else None, _now()),
            )

    def save_event(self, event: EventPage, fights: list[tuple[int, FightPage]], complete: bool) -> None:
        """Write an event and all its fights in one transaction."""
        with self.conn:
            self.conn.execute(
                """INSERT INTO events(event_id, name, date, location, is_complete, scraped_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET name=excluded.name, date=excluded.date,
                     location=excluded.location, is_complete=excluded.is_complete,
                     scraped_at=excluded.scraped_at""",
                (event.event_id, event.name, event.date.isoformat() if event.date else None,
                 event.location, int(complete), _now()),
            )
            for bout_order, f in fights:
                self.conn.execute("DELETE FROM fight_stats WHERE fight_id = ?", (f.fight_id,))
                self.conn.execute(
                    """INSERT OR REPLACE INTO fights(fight_id, event_id, date, bout_order,
                         fighter1_id, fighter2_id, fighter1_status, fighter2_status, winner_id,
                         outcome, bout_type, weight_class, title_bout, women, method, method_group,
                         method_detail, end_round, end_time_sec, time_format, scheduled_rounds,
                         fight_duration_sec, referee)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f.fight_id, event.event_id, event.date.isoformat() if event.date else None,
                     bout_order, f.fighter1_id, f.fighter2_id, f.fighter1_status, f.fighter2_status,
                     f.winner_id, f.outcome, f.bout_type, f.weight_class, int(f.title_bout),
                     int(f.women), f.method, f.method_group, f.method_detail, f.end_round,
                     f.end_time_sec, f.time_format, f.scheduled_rounds, f.fight_duration_sec,
                     f.referee),
                )
                for fighter_id, rounds in f.stats.items():
                    for rnd, line in rounds.items():
                        vals = asdict(line)
                        self.conn.execute(
                            f"INSERT INTO fight_stats(fight_id, fighter_id, round, {', '.join(STAT_COLUMNS)}) "
                            f"VALUES (?,?,?,{', '.join('?' * len(STAT_COLUMNS))})",
                            (f.fight_id, fighter_id, rnd, *[vals[c] for c in STAT_COLUMNS]),
                        )

    def replace_history(self, events: list[tuple], fights: list[tuple], stats: list[tuple],
                        fighters: list[tuple]) -> None:
        """Rebuild all historical tables in one transaction (used by the CSV loader)."""
        with self.conn:
            for table in ("fight_stats", "fights", "events", "fighters"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", events)
            self.conn.executemany(f"INSERT INTO fights VALUES ({','.join('?' * 23)})", fights)
            self.conn.executemany(
                f"INSERT OR REPLACE INTO fight_stats VALUES ({','.join('?' * (3 + len(STAT_COLUMNS)))})", stats)
            self.conn.executemany("INSERT OR REPLACE INTO fighters VALUES (?,?,?,?,?,?,?)", fighters)

    def replace_upcoming(self, events: list[EventPage]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM upcoming_bouts")
            for ev in events:
                for b in ev.bouts:
                    self.conn.execute(
                        """INSERT INTO upcoming_bouts(event_id, event_name, date, bout_order, fight_id,
                             fighter1_id, fighter1_name, fighter2_id, fighter2_name, weight_class,
                             scraped_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (ev.event_id, ev.name, ev.date.isoformat() if ev.date else None, b.bout_order,
                         b.fight_id, b.fighter1_id, b.fighter1_name, b.fighter2_id, b.fighter2_name,
                         b.weight_class, _now()),
                    )

    def summary(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "events": q("SELECT COUNT(*) FROM events WHERE is_complete = 1"),
            "fights": q("SELECT COUNT(*) FROM fights"),
            "fighters": q("SELECT COUNT(*) FROM fighters"),
            "fights_with_stats": q("SELECT COUNT(DISTINCT fight_id) FROM fight_stats WHERE round = 0"),
            "first_date": q("SELECT MIN(date) FROM fights"),
            "last_date": q("SELECT MAX(date) FROM fights"),
            "upcoming_bouts": q("SELECT COUNT(*) FROM upcoming_bouts"),
        }
