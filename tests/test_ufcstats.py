"""UFCStats parser and crawler tests, run offline against saved pages."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ufc_edge.data import ufcstats_parse as P
from ufc_edge.data.http import FetchError, Fetcher
from ufc_edge.data.store import StatsStore
from ufc_edge.data.ufcstats import scrape_completed

FIX = Path(__file__).parent / "fixtures" / "ufcstats"


def read(rel: str) -> str:
    return (FIX / rel).read_text(encoding="utf-8")


class TestHelpers(unittest.TestCase):
    def test_small_parsers(self) -> None:
        self.assertEqual(P.parse_clock("4:59"), 299)
        self.assertIsNone(P.parse_clock("--"))
        self.assertEqual(P.parse_x_of_y("35 of 80"), (35, 80))
        self.assertEqual(P.parse_x_of_y("---"), (None, None))
        self.assertEqual(P.parse_height_cm("5' 11\""), 180.3)
        self.assertEqual(P.parse_reach_cm('72"'), 182.9)
        self.assertIsNone(P.parse_reach_cm("--"))
        self.assertEqual(P.parse_date("Jul 22, 1990"), date(1990, 7, 22))
        self.assertEqual(P.parse_date("September 06, 2026"), date(2026, 9, 6))

    def test_time_format_and_duration(self) -> None:
        self.assertEqual(P.parse_time_format("3 Rnd (5-5-5)"), (3, [300, 300, 300]))
        self.assertEqual(P.parse_time_format("No Time Limit"), (None, []))
        self.assertEqual(P.fight_duration(2, 221, [300, 300, 300]), 521)
        self.assertIsNone(P.fight_duration(None, 221, [300]))

    def test_method_and_weight_class(self) -> None:
        self.assertEqual(P.method_group("KO/TKO"), "KO_TKO")
        self.assertEqual(P.method_group("TKO - Doctor's Stoppage"), "KO_TKO")
        self.assertEqual(P.method_group("Submission"), "SUB")
        self.assertEqual(P.method_group("Decision - Split"), "DEC")
        self.assertEqual(P.method_group("Overturned"), "OTHER")
        self.assertEqual(P.split_weight_class("UFC Women's Flyweight Title Bout"), ("Women's Flyweight", True, True))
        self.assertEqual(P.split_weight_class("Lightweight Bout"), ("Lightweight", False, False))


class TestPageParsers(unittest.TestCase):
    def test_event_list(self) -> None:
        events = P.parse_event_list(read("lists/events_completed.html"))
        self.assertEqual([e.event_id for e in events], ["evt002", "evt001", "evtfuture"])
        self.assertEqual(events[0].date, date(2026, 10, 18))
        self.assertEqual(events[1].location, "Paris, Ile-de-France, France")

    def test_event(self) -> None:
        ev = P.parse_event(read("event/evt001.html"), "evt001")
        self.assertEqual(ev.name, "UFC 999: Gamma vs. Delta")
        self.assertEqual(ev.date, date(2026, 9, 6))
        self.assertEqual(len(ev.bouts), 2)
        b = ev.bouts[0]
        self.assertEqual((b.bout_order, b.fight_id, b.fighter1_id, b.fighter2_name, b.weight_class),
                         (0, "ftA", "fA", "Ben Baker", "Lightweight"))

    def test_fight_ko(self) -> None:
        f = P.parse_fight(read("fight/ftA.html"), "ftA")
        self.assertEqual((f.fighter1_id, f.fighter1_status, f.fighter2_status), ("fA", "W", "L"))
        self.assertEqual(f.winner_id, "fA")
        self.assertEqual(f.outcome, "WIN")
        self.assertEqual((f.method, f.method_group, f.end_round, f.end_time_sec), ("KO/TKO", "KO_TKO", 2, 221))
        self.assertEqual((f.scheduled_rounds, f.fight_duration_sec, f.referee), (3, 521, "Herb Dean"))
        self.assertEqual(f.method_detail, "Punch to Head At Distance")
        total = f.stats["fA"][0]
        self.assertEqual((total.kd, total.sig_landed, total.sig_att, total.ctrl_sec), (1, 40, 75, 30))
        self.assertEqual(f.stats["fB"][0].td_landed, 1)
        self.assertEqual(sorted(f.stats["fA"]), [0, 1, 2])  # whole fight + 2 rounds
        self.assertEqual(f.stats["fA"][2].kd, 1)
        self.assertEqual(f.stats["fA"][0].head_landed, 40)

    def test_title_fight(self) -> None:
        f = P.parse_fight(read("fight/ftC.html"), "ftC")
        self.assertTrue(f.title_bout)
        self.assertEqual((f.weight_class, f.scheduled_rounds, f.method_group), ("Lightweight", 5, "SUB"))

    def test_fighter(self) -> None:
        a = P.parse_fighter(read("fighter/fA.html"), "fA")
        self.assertEqual((a.name, a.height_cm, a.reach_cm, a.stance, a.dob),
                         ("Alan Able", 177.8, 182.9, "Orthodox", date(1990, 7, 22)))
        d = P.parse_fighter(read("fighter/fD.html"), "fD")
        self.assertEqual((d.height_cm, d.reach_cm, d.stance, d.dob), (None, None, None, None))


class TestCrawler(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(FIX, self.tmp / "raw")
        self.store = StatsStore(self.tmp / "ufc.db")
        self.fetcher = Fetcher(self.tmp / "raw", offline=True)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_crawl_from_cache(self) -> None:
        report = scrape_completed(self.store, self.fetcher, today=date(2026, 10, 27), log=lambda _: None)
        self.assertEqual(report.errors, [])
        self.assertEqual((report.events_saved, report.fights_saved, report.fighters_saved), (2, 4, 4))
        s = self.store.summary()
        self.assertEqual((s["events"], s["fights"], s["fighters"], s["fights_with_stats"]), (2, 4, 4, 4))
        self.assertEqual((s["first_date"], s["last_date"]), ("2026-09-06", "2026-10-18"))
        # Future events on the list are ignored.
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM events WHERE event_id = 'evtfuture'").fetchone())
        # Second run skips completed events.
        again = scrape_completed(self.store, self.fetcher, today=date(2026, 10, 27), log=lambda _: None)
        self.assertEqual((again.events_saved, again.events_skipped), (0, 2))

    def test_max_events_takes_most_recent(self) -> None:
        scrape_completed(self.store, self.fetcher, max_events=1, today=date(2026, 10, 27), log=lambda _: None)
        ids = [r[0] for r in self.store.conn.execute("SELECT event_id FROM events")]
        self.assertEqual(ids, ["evt002"])

    def test_missing_page_is_reported_not_fatal(self) -> None:
        (self.tmp / "raw" / "fight" / "ftB.html").unlink()
        report = scrape_completed(self.store, self.fetcher, today=date(2026, 10, 27), log=lambda _: None)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("ftB", report.errors[0])
        # The event with the missing fight stays incomplete, so it's retried next run.
        self.assertFalse(self.store.event_is_complete("evt001"))
        self.assertTrue(self.store.event_is_complete("evt002"))

    def test_offline_without_cache_raises(self) -> None:
        with self.assertRaises(FetchError):
            Fetcher(self.tmp / "empty", offline=True).get("http://x", "nope.html")


if __name__ == "__main__":
    unittest.main()
