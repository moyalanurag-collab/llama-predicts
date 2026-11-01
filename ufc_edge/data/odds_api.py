"""The Odds API client: timestamped UK fight-winner odds snapshots.

Each run stores every bookmaker's current price for every upcoming MMA fight,
with two timestamps:

* ``captured_at``: when we downloaded it
* ``bookmaker_last_update``: when the bookmaker last changed the price

Snapshots are only ever added, never edited, so over time this becomes a
leakage-free odds history for paper trading and closing-line value.

The API covers all MMA (UFC, PFL and others) under one sport key. Fights are
matched to UFC cards by fighter name in a later step.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..errors import UfcEdgeError

API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "mma_mixed_martial_arts"

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id                    INTEGER PRIMARY KEY,
    captured_at           TEXT NOT NULL,
    api_event_id          TEXT NOT NULL,
    commence_time         TEXT NOT NULL,
    home_team             TEXT NOT NULL,
    away_team             TEXT NOT NULL,
    bookmaker_key         TEXT NOT NULL,
    bookmaker_title       TEXT NOT NULL,
    bookmaker_last_update TEXT,
    market                TEXT NOT NULL,
    outcome_name          TEXT NOT NULL,
    price                 REAL NOT NULL CHECK (price > 1),
    UNIQUE (api_event_id, bookmaker_key, market, outcome_name, bookmaker_last_update)
);
CREATE INDEX IF NOT EXISTS ix_odds_event ON odds_snapshots(api_event_id, captured_at);
CREATE TRIGGER IF NOT EXISTS trg_odds_no_update BEFORE UPDATE ON odds_snapshots
BEGIN SELECT RAISE(ABORT, 'odds_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_odds_no_delete BEFORE DELETE ON odds_snapshots
BEGIN SELECT RAISE(ABORT, 'odds_snapshots is append-only'); END;
"""

# opener(url) -> (body bytes, headers dict). Swappable for tests.
Opener = Callable[[str], tuple[bytes, dict[str, str]]]


class OddsApiError(UfcEdgeError):
    pass


def load_api_key(env_file: str | Path = ".env") -> str:
    """Read ODDS_API_KEY from the environment or a local .env file."""
    key = os.environ.get("ODDS_API_KEY", "").strip()
    path = Path(env_file)
    if not key and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ODDS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise OddsApiError("No ODDS_API_KEY found. Copy .env.example to .env and add your key.")
    return key


def _default_opener(url: str) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": "ufc-edge/0.2"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), {k.lower(): v for k, v in resp.headers.items()}


@dataclass(frozen=True)
class SnapshotResult:
    captured_at: str
    events: int
    prices_saved: int
    requests_remaining: str | None
    raw_path: Path | None


class OddsClient:
    def __init__(self, api_key: str, opener: Opener = _default_opener) -> None:
        self._key = api_key
        self._opener = opener

    def _get(self, path: str, params: dict[str, str]) -> tuple[list | dict, dict[str, str]]:
        query = urllib.parse.urlencode({**params, "apiKey": self._key})
        try:
            body, headers = self._opener(f"{API_BASE}{path}?{query}")
        except urllib.error.HTTPError as exc:
            # Never include the URL: it contains the API key.
            hint = {401: "API key rejected", 422: "invalid request", 429: "quota exhausted"}.get(exc.code, "")
            raise OddsApiError(f"The Odds API returned HTTP {exc.code} {hint}".strip()) from None
        except urllib.error.URLError as exc:
            raise OddsApiError(f"Could not reach The Odds API: {exc.reason}") from None
        return json.loads(body), headers

    def fetch_mma_odds(self, regions: str = "uk") -> tuple[list[dict], dict[str, str]]:
        data, headers = self._get(
            f"/sports/{SPORT_KEY}/odds",
            {"regions": regions, "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"},
        )
        if not isinstance(data, list):
            raise OddsApiError(f"Unexpected response: {str(data)[:200]}")
        return data, headers


def init_odds_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def save_snapshot(conn: sqlite3.Connection, events: list[dict], captured_at: str) -> int:
    """Store every price in the response. Returns how many new rows were written."""
    init_odds_schema(conn)
    before = conn.total_changes
    with conn:
        for ev in events:
            for bk in ev.get("bookmakers", []):
                for mk in bk.get("markets", []):
                    if mk.get("key") != "h2h":
                        continue
                    for out in mk.get("outcomes", []):
                        price = out.get("price")
                        if not isinstance(price, (int, float)) or price <= 1:
                            continue  # skip malformed prices rather than storing junk
                        conn.execute(
                            """INSERT OR IGNORE INTO odds_snapshots(captured_at, api_event_id,
                                 commence_time, home_team, away_team, bookmaker_key, bookmaker_title,
                                 bookmaker_last_update, market, outcome_name, price)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (captured_at, ev["id"], ev["commence_time"], ev["home_team"],
                             ev["away_team"], bk["key"], bk.get("title", bk["key"]),
                             mk.get("last_update") or bk.get("last_update"), "h2h",
                             out["name"], float(price)),
                        )
    return conn.total_changes - before


def take_snapshot(
    conn: sqlite3.Connection,
    client: OddsClient,
    raw_dir: str | Path | None = None,
    now: datetime | None = None,
) -> SnapshotResult:
    now = now or datetime.now(timezone.utc)
    captured_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    events, headers = client.fetch_mma_odds()
    raw_path = None
    if raw_dir is not None:
        raw_path = Path(raw_dir) / f"mma_odds_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(events, indent=1), encoding="utf-8")
    saved = save_snapshot(conn, events, captured_at)
    return SnapshotResult(captured_at, len(events), saved, headers.get("x-requests-remaining"), raw_path)
