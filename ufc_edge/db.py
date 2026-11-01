"""Database schema and connection handling (SQLite).

Protections that live in the database itself, so a bug elsewhere in the
code cannot bypass them:

* The ledger, authorisations, risk-config history and audit log are
  append-only. Triggers abort any UPDATE or DELETE.
* A bet can be settled exactly once, and its stake and odds can never change.
* One non-void bet per (event, fight, market, selection), plus a unique
  idempotency key, so retries and restarts cannot create duplicate wagers.
* The BANKROLL EXHAUSTED latch cannot be cleared unless the same update
  references a *new* manual authorisation record.

Paper trading and live tracking use separate database files, so the two
can never mix.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Manual authorisations: initial bankroll, new bankroll, risk changes,
-- kill-switch release, clearing a halt.
CREATE TABLE IF NOT EXISTS authorisations (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN (
                   'INITIAL_BANKROLL','NEW_BANKROLL','RISK_CHANGE',
                   'KILL_SWITCH_RELEASE','HALT_CLEAR')),
    operator   TEXT NOT NULL CHECK (length(trim(operator)) > 0),
    reason     TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    payload    TEXT NOT NULL DEFAULT '{}'
);

-- Single-row system state.
CREATE TABLE IF NOT EXISTS system_state (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    mode                   TEXT NOT NULL CHECK (mode IN ('paper','live')),
    status                 TEXT NOT NULL CHECK (status IN ('UNINITIALISED','ACTIVE','EXHAUSTED')),
    bankroll_cap_pence     INTEGER NOT NULL CHECK (bankroll_cap_pence >= 0),
    kill_switch            INTEGER NOT NULL CHECK (kill_switch IN (0,1)),
    kill_reason            TEXT,
    halted                 INTEGER NOT NULL CHECK (halted IN (0,1)),
    halt_reason            TEXT,
    last_authorisation_id  INTEGER REFERENCES authorisations(id),
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_config_history (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT NOT NULL,
    authorisation_id INTEGER REFERENCES authorisations(id),
    config_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    starts_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bets (
    id                INTEGER PRIMARY KEY,
    idempotency_key   TEXT NOT NULL UNIQUE,
    event_id          TEXT NOT NULL REFERENCES events(id),
    fight_id          TEXT NOT NULL,
    market            TEXT NOT NULL,
    selection         TEXT NOT NULL,
    decimal_odds      TEXT NOT NULL,
    stake_pence       INTEGER NOT NULL CHECK (stake_pence > 0),
    odds_source       TEXT NOT NULL,
    odds_captured_at  TEXT NOT NULL,
    model_probability REAL NOT NULL CHECK (model_probability > 0 AND model_probability < 1),
    placed_at         TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('OPEN','WON','LOST','VOID')),
    settled_at        TEXT,
    result_source     TEXT,
    returned_pence    INTEGER CHECK (returned_pence IS NULL OR returned_pence >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_bets_one_per_selection
    ON bets(event_id, fight_id, market, selection) WHERE status <> 'VOID';

-- Double-entry ledger: each transaction's lines sum to zero.
CREATE TABLE IF NOT EXISTS ledger_transactions (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN (
                   'DEPOSIT','BET_PLACED','BET_WON','BET_LOST','BET_VOID',
                   'PROFIT_SWEEP','PROTECTED_TRANSFER_OUT')),
    bet_id     INTEGER REFERENCES bets(id),
    authorisation_id INTEGER REFERENCES authorisations(id),
    memo       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ledger_lines (
    id           INTEGER PRIMARY KEY,
    txn_id       INTEGER NOT NULL REFERENCES ledger_transactions(id),
    account      TEXT NOT NULL CHECK (account IN (
                     'OWNER','ACTIVE','OPEN_STAKES','PROTECTED','WITHDRAWN','MARKET')),
    amount_pence INTEGER NOT NULL CHECK (amount_pence <> 0)
);

CREATE INDEX IF NOT EXISTS ix_ledger_lines_txn ON ledger_lines(txn_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor      TEXT NOT NULL CHECK (actor IN ('system','agent','operator')),
    action     TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}'
);

-- Non-financial context for each bet (who, when, model view, closing line).
-- Money never lives here; the ledger is the only source of balances.
CREATE TABLE IF NOT EXISTS bet_context (
    bet_id          INTEGER PRIMARY KEY REFERENCES bets(id),
    api_event_id    TEXT,
    commence_time   TEXT,
    fighter_id      TEXT,
    fighter_name    TEXT,
    opponent_id     TEXT,
    opponent_name   TEXT,
    p_model         REAL,
    p_market        REAL,
    ev_at_bet       REAL,
    bookmaker       TEXT,
    closing_odds    REAL,
    clv             REAL       -- bet odds / closing odds - 1 (positive = beat the closing line)
);

-- Append-only enforcement --------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_ledger_txn_no_update BEFORE UPDATE ON ledger_transactions
BEGIN SELECT RAISE(ABORT, 'ledger_transactions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_txn_no_delete BEFORE DELETE ON ledger_transactions
BEGIN SELECT RAISE(ABORT, 'ledger_transactions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_lines_no_update BEFORE UPDATE ON ledger_lines
BEGIN SELECT RAISE(ABORT, 'ledger_lines is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_lines_no_delete BEFORE DELETE ON ledger_lines
BEGIN SELECT RAISE(ABORT, 'ledger_lines is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_auth_no_update BEFORE UPDATE ON authorisations
BEGIN SELECT RAISE(ABORT, 'authorisations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_auth_no_delete BEFORE DELETE ON authorisations
BEGIN SELECT RAISE(ABORT, 'authorisations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_risk_cfg_no_update BEFORE UPDATE ON risk_config_history
BEGIN SELECT RAISE(ABORT, 'risk_config_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_risk_cfg_no_delete BEFORE DELETE ON risk_config_history
BEGIN SELECT RAISE(ABORT, 'risk_config_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- Bets: no deletes; settle once; stake/odds/selection immutable.
CREATE TRIGGER IF NOT EXISTS trg_bets_no_delete BEFORE DELETE ON bets
BEGIN SELECT RAISE(ABORT, 'bets cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS trg_bets_settle_once BEFORE UPDATE ON bets
WHEN OLD.status <> 'OPEN'
BEGIN SELECT RAISE(ABORT, 'bet is already settled'); END;
CREATE TRIGGER IF NOT EXISTS trg_bets_immutable_terms BEFORE UPDATE ON bets
WHEN NEW.stake_pence IS NOT OLD.stake_pence
  OR NEW.decimal_odds IS NOT OLD.decimal_odds
  OR NEW.selection IS NOT OLD.selection
  OR NEW.market IS NOT OLD.market
  OR NEW.event_id IS NOT OLD.event_id
  OR NEW.fight_id IS NOT OLD.fight_id
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
BEGIN SELECT RAISE(ABORT, 'bet terms are immutable'); END;

-- The exhaustion latch can only be cleared with a new authorisation.
CREATE TRIGGER IF NOT EXISTS trg_state_exhaustion_latch BEFORE UPDATE ON system_state
WHEN OLD.status = 'EXHAUSTED' AND NEW.status <> 'EXHAUSTED'
  AND NEW.last_authorisation_id IS OLD.last_authorisation_id
BEGIN SELECT RAISE(ABORT, 'manual authorisation required to leave EXHAUSTED'); END;
CREATE TRIGGER IF NOT EXISTS trg_state_no_delete BEFORE DELETE ON system_state
BEGIN SELECT RAISE(ABORT, 'system_state cannot be deleted'); END;
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with safe settings.

    ``isolation_level=None`` hands transaction control to our code, which
    uses ``BEGIN IMMEDIATE`` so that checking limits and writing a bet happen
    atomically under SQLite's write lock (see ``transaction``).
    """
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_schema(conn: sqlite3.Connection, mode: str, now_iso: str) -> None:
    """Create tables if missing and the single system_state row."""
    if mode not in ("paper", "live"):
        raise ValueError("mode must be 'paper' or 'live'")
    conn.executescript(SCHEMA)
    try:  # migration: experiment flag added after the first release
        conn.execute("ALTER TABLE bet_context ADD COLUMN beyond_limits TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        row = conn.execute("SELECT mode FROM system_state WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO system_state
                   (id, mode, status, bankroll_cap_pence, kill_switch, halted, updated_at)
                   VALUES (1, ?, 'UNINITIALISED', 0, 0, 0, ?)""",
                (mode, now_iso),
            )
        elif row["mode"] != mode:
            raise ValueError(f"Database was created in {row['mode']!r} mode, not {mode!r}.")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
