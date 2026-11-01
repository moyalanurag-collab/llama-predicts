"""Odds API client and name-matching tests (no network)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ufc_edge.data.names import normalise_name, same_fighter
from ufc_edge.data.odds_api import OddsApiError, OddsClient, load_api_key, save_snapshot, take_snapshot

SAMPLE = [
    {
        "id": "abc123",
        "sport_key": "mma_mixed_martial_arts",
        "commence_time": "2026-11-08T03:00:00Z",
        "home_team": "Alan Able",
        "away_team": "Ben Baker",
        "bookmakers": [
            {"key": "williamhill", "title": "William Hill", "last_update": "2026-11-07T10:00:00Z",
             "markets": [{"key": "h2h", "last_update": "2026-11-07T10:00:00Z",
                          "outcomes": [{"name": "Alan Able", "price": 1.62},
                                       {"name": "Ben Baker", "price": 2.30}]}]},
            {"key": "skybet", "title": "Sky Bet", "last_update": "2026-11-07T09:55:00Z",
             "markets": [{"key": "h2h", "last_update": "2026-11-07T09:55:00Z",
                          "outcomes": [{"name": "Alan Able", "price": 1.57},
                                       {"name": "Ben Baker", "price": "bad"}]}]},
        ],
    }
]


def fake_opener(seen: list[str]):  # type: ignore[no-untyped-def]
    def opener(url: str):  # type: ignore[no-untyped-def]
        seen.append(url)
        return json.dumps(SAMPLE).encode(), {"x-requests-remaining": "487"}
    return opener


class TestOddsApi(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_snapshot_saves_valid_prices_only(self) -> None:
        seen: list[str] = []
        r = take_snapshot(self.conn, OddsClient("SECRET", fake_opener(seen)),
                          now=datetime(2026, 11, 7, 10, 5, tzinfo=timezone.utc))
        self.assertEqual((r.events, r.prices_saved, r.requests_remaining), (1, 3, "487"))
        self.assertIn("regions=uk", seen[0])
        self.assertIn("oddsFormat=decimal", seen[0])
        rows = self.conn.execute(
            "SELECT bookmaker_key, outcome_name, price, captured_at FROM odds_snapshots ORDER BY id"
        ).fetchall()
        self.assertEqual(rows[0], ("williamhill", "Alan Able", 1.62, "2026-11-07T10:05:00Z"))

    def test_repeat_snapshot_does_not_duplicate_unchanged_prices(self) -> None:
        save_snapshot(self.conn, SAMPLE, "2026-11-07T10:05:00Z")
        self.assertEqual(save_snapshot(self.conn, SAMPLE, "2026-11-07T10:20:00Z"), 0)

    def test_snapshots_are_append_only(self) -> None:
        save_snapshot(self.conn, SAMPLE, "2026-11-07T10:05:00Z")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE odds_snapshots SET price = 9.0")

    def test_errors_never_leak_the_key(self) -> None:
        def opener(url: str):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        with self.assertRaises(OddsApiError) as ctx:
            OddsClient("SECRET-KEY", opener).fetch_mma_odds()
        self.assertNotIn("SECRET-KEY", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))

    def test_load_key_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=True):
            env = Path(d) / ".env"
            env.write_text("# comment\nODDS_API_KEY=abc123\n", encoding="utf-8")
            self.assertEqual(load_api_key(env), "abc123")
            env.write_text("ODDS_API_KEY=\n", encoding="utf-8")
            with self.assertRaises(OddsApiError):
                load_api_key(env)


class TestNames(unittest.TestCase):
    def test_normalise(self) -> None:
        self.assertEqual(normalise_name("José Aldo Jr."), "jose aldo")
        self.assertEqual(normalise_name("Khamzat  Chimaev"), "khamzat chimaev")
        self.assertEqual(normalise_name("Marc-André Barriault"), "marc andre barriault")

    def test_matching(self) -> None:
        self.assertTrue(same_fighter("Jose Aldo", "José Aldo Jr."))
        self.assertTrue(same_fighter("Marc-Andre Barriault", "Marc André Barriault"))
        self.assertTrue(same_fighter("Rafael dos Anjos", "Rafael Dos Anjos"))
        self.assertTrue(same_fighter("Weili Zhang", "Zhang Weili"))
        self.assertFalse(same_fighter("Nate Diaz", "Nick Diaz"))


if __name__ == "__main__":
    unittest.main()
