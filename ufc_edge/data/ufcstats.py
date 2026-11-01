"""Direct UFCStats crawler. **Not used.**

As of October 2026, UFCStats.com shows automated clients a JavaScript
browser-verification page. Getting past that would mean bypassing an access
control, so this crawler is not wired into the CLI. Historical data comes from
the public CSV export instead (see ``ufcstats_csv.py``). The code is kept in
case the site offers sanctioned access again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from .http import FetchError, Fetcher
from .store import StatsStore
from .ufcstats_parse import (
    COMPLETED_EVENTS_URL,
    UPCOMING_EVENTS_URL,
    EventPage,
    FightPage,
    ParseError,
    parse_event,
    parse_event_list,
    parse_fight,
    parse_fighter,
)


@dataclass
class CrawlReport:
    events_saved: int = 0
    events_skipped: int = 0
    fights_saved: int = 0
    fighters_saved: int = 0
    errors: list[str] = field(default_factory=list)


def _log_errors(cache_dir: Path, errors: list[str]) -> None:
    if errors:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_dir / "errors.log", "a", encoding="utf-8") as fh:
            fh.write("\n".join(errors) + "\n")


def scrape_completed(
    store: StatsStore,
    fetcher: Fetcher,
    max_events: int | None = None,
    refresh: bool = False,
    log: Callable[[str], None] = print,
    today: date | None = None,
) -> CrawlReport:
    """Scrape completed events. ``max_events`` takes the N most recent."""
    today = today or date.today()
    report = CrawlReport()
    stubs = parse_event_list(fetcher.get(COMPLETED_EVENTS_URL, "lists/events_completed.html", refresh=True))
    stubs = [s for s in stubs if s.date is not None and s.date < today]
    stubs.sort(key=lambda s: s.date, reverse=True)  # type: ignore[arg-type, return-value]
    if max_events is not None:
        stubs = stubs[:max_events]
    stubs.reverse()  # oldest first

    for i, stub in enumerate(stubs, 1):
        if not refresh and store.event_is_complete(stub.event_id):
            report.events_skipped += 1
            continue
        log(f"[{i}/{len(stubs)}] {stub.date}  {stub.name}")
        try:
            event = parse_event(fetcher.get(stub.url, f"event/{stub.event_id}.html"), stub.event_id)
        except (FetchError, ParseError) as exc:
            report.errors.append(f"event {stub.event_id} ({stub.name}): {exc}")
            continue
        if event.date is None:
            event = EventPage(event.event_id, event.name, stub.date, event.location or stub.location, event.bouts)

        fights: list[tuple[int, FightPage]] = []
        complete = True
        for bout in event.bouts:
            if not bout.fight_url or not bout.fight_id:
                report.errors.append(f"event {stub.event_id}: bout {bout.bout_order} has no fight link")
                complete = False
                continue
            try:
                fight = parse_fight(fetcher.get(bout.fight_url, f"fight/{bout.fight_id}.html"), bout.fight_id)
            except (FetchError, ParseError) as exc:
                report.errors.append(f"fight {bout.fight_id} ({bout.fighter1_name} vs {bout.fighter2_name}): {exc}")
                complete = False
                continue
            fights.append((bout.bout_order, fight))
            for fid in (fight.fighter1_id, fight.fighter2_id):
                if store.has_fighter(fid):
                    continue
                try:
                    url = f"http://ufcstats.com/fighter-details/{fid}"
                    store.save_fighter(parse_fighter(fetcher.get(url, f"fighter/{fid}.html"), fid))
                    report.fighters_saved += 1
                except (FetchError, ParseError) as exc:
                    report.errors.append(f"fighter {fid}: {exc}")

        store.save_event(event, fights, complete=complete and bool(fights))
        report.events_saved += 1
        report.fights_saved += len(fights)

    _log_errors(fetcher.cache_dir, report.errors)
    return report


def scrape_upcoming(store: StatsStore, fetcher: Fetcher, log: Callable[[str], None] = print) -> CrawlReport:
    """Refresh scheduled cards. Always re-downloads, because cards change."""
    report = CrawlReport()
    stubs = parse_event_list(fetcher.get(UPCOMING_EVENTS_URL, "lists/events_upcoming.html", refresh=True))
    events: list[EventPage] = []
    for stub in stubs:
        log(f"upcoming: {stub.date}  {stub.name}")
        try:
            ev = parse_event(fetcher.get(stub.url, f"upcoming/{stub.event_id}.html", refresh=True), stub.event_id)
            events.append(ev if ev.date else EventPage(ev.event_id, ev.name, stub.date, ev.location, ev.bouts))
            report.events_saved += 1
        except (FetchError, ParseError) as exc:
            report.errors.append(f"upcoming event {stub.event_id}: {exc}")
    store.replace_upcoming(events)
    _log_errors(fetcher.cache_dir, report.errors)
    return report
