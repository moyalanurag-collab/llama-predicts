"""Load UFCStats history from the public `scrape_ufc_stats` CSV files.

UFCStats.com now shows automated visitors a JavaScript browser challenge.
Getting around that would mean bypassing an access control, so this project
doesn't do it. Instead it uses the CSV export maintained at
https://github.com/Greco1899/scrape_ufc_stats. That project publishes the
complete UFCStats history (events, results, round-by-round stats and fighter
"tale of the tape") and refreshes it automatically after each event.

The loader rebuilds the ``events``, ``fights``, ``fight_stats`` and
``fighters`` tables in ``data/ufc.db``, in the same shape the feature builder
already uses.

Fighter identity
----------------
The fight and stats files name fighters but don't give their UFCStats ids.
Names are matched to the fighter file like this:

1. Exact name or normalised name ("Cortes Acosta" = "Cortes-Acosta", "Jr." dropped).
2. Different fighters who share a name (for example, two "Bruno Silva"s) are
   told apart by the bout's weight class against each fighter's listed weight.
3. A fighter missing from the fighter file gets a stable placeholder id
   ``name:<normalised name>``, so his fights still link together.

Every non-exact match is counted in the load report.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .http import Fetcher
from .names import fuzzy_unique_match, name_keys, normalise_name
from .store import STAT_COLUMNS, StatsStore
from .ufcstats_parse import (
    id_from_url,
    method_group,
    parse_clock,
    parse_date,
    parse_height_cm,
    parse_reach_cm,
    parse_time_format,
    parse_x_of_y,
    fight_duration,
    split_weight_class,
)

RAW_BASE = "https://raw.githubusercontent.com/Greco1899/scrape_ufc_stats/main"
FILES = ["ufc_event_details.csv", "ufc_fight_results.csv", "ufc_fight_stats.csv", "ufc_fighter_tott.csv"]
SOURCE_NOTE = "UFCStats data via github.com/Greco1899/scrape_ufc_stats"

WEIGHT_CLASS_LBS = {
    "strawweight": 115, "flyweight": 125, "bantamweight": 135, "featherweight": 145,
    "lightweight": 155, "welterweight": 170, "middleweight": 185, "light heavyweight": 205,
    "heavyweight": 265,
}


@dataclass
class LoadReport:
    events: int = 0
    fights: int = 0
    stat_rows: int = 0
    fighters: int = 0
    matched_normalised: set[str] = field(default_factory=set)
    namesakes_resolved: dict[str, int] = field(default_factory=dict)
    placeholders: set[str] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)
    duplicates: int = 0  # same fight listed twice under an old and new event name
    newest_event: str | None = None


def download_csvs(fetcher: Fetcher) -> Path:
    """Fetch the latest CSVs (always refreshed). Returns the folder they're in."""
    for name in FILES:
        fetcher.get(f"{RAW_BASE}/{name}", f"{name}", refresh=True)
    return fetcher.cache_dir


def _read(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]


def _weight_lbs(text: str) -> float | None:
    m = re.search(r"(\d+)\s*lbs", text or "")
    return float(m.group(1)) if m else None


def _class_lbs(weight_class: str) -> float | None:
    wc = weight_class.lower()
    for name in sorted(WEIGHT_CLASS_LBS, key=len, reverse=True):  # "light heavyweight" before "heavyweight"
        if name in wc:
            return WEIGHT_CLASS_LBS[name]
    return None


class FighterResolver:
    def __init__(self, tott: list[dict[str, str]], report: LoadReport) -> None:
        self.report = report
        self.by_exact: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.by_norm: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.all = [(id_from_url(r["URL"]), r["FIGHTER"]) for r in tott]
        self.by_id = {id_from_url(r["URL"]): r for r in tott}
        for row in tott:
            self.by_exact[row["FIGHTER"]].append(row)
            self.by_norm[normalise_name(row["FIGHTER"])].append(row)
            for k in name_keys(row["FIGHTER"]):
                self.by_key[k].append(row)

    def resolve(self, name: str, weight_class: str) -> str:
        candidates = self.by_exact.get(name)
        if not candidates:
            candidates = self.by_norm.get(normalise_name(name))
        if not candidates:  # nickname / name-order keys (Zach = Zachary)
            seen: dict[str, dict[str, str]] = {}
            for k in name_keys(name):
                for row in self.by_key.get(k, []):
                    seen[row["URL"]] = row
            candidates = list(seen.values()) or None
        if not candidates:  # unique same-surname, compatible-first-name candidate
            fid = fuzzy_unique_match(name, self.all)
            candidates = [self.by_id[fid]] if fid else None
        if candidates and name not in self.by_exact:
            self.report.matched_normalised.add(name)
        if not candidates:
            self.report.placeholders.add(name)
            return f"name:{normalise_name(name)}"
        if len(candidates) == 1:
            return id_from_url(candidates[0]["URL"])
        target = _class_lbs(weight_class)

        def distance(row: dict[str, str]) -> float:
            w = _weight_lbs(row.get("WEIGHT", ""))
            if target is None or w is None:
                return 50.0
            return abs(w - target)

        best = min(candidates, key=distance)
        self.report.namesakes_resolved[name] = self.report.namesakes_resolved.get(name, 0) + 1
        return id_from_url(best["URL"])


def _stat_line(row: dict[str, str]) -> dict[str, int | None]:
    v: dict[str, int | None] = {c: None for c in STAT_COLUMNS}
    kd = row.get("KD", "")
    v["kd"] = int(kd) if kd.isdigit() else None
    v["sig_landed"], v["sig_att"] = parse_x_of_y(row.get("SIG.STR.", ""))
    v["total_landed"], v["total_att"] = parse_x_of_y(row.get("TOTAL STR.", ""))
    v["td_landed"], v["td_att"] = parse_x_of_y(row.get("TD", ""))
    sub = row.get("SUB.ATT", "")
    v["sub_att"] = int(sub) if sub.isdigit() else None
    rev = row.get("REV.", "")
    v["rev"] = int(rev) if rev.isdigit() else None
    v["ctrl_sec"] = parse_clock(row.get("CTRL", ""))
    for zone in ("HEAD", "BODY", "LEG", "DISTANCE", "CLINCH", "GROUND"):
        v[f"{zone.lower()}_landed"], v[f"{zone.lower()}_att"] = parse_x_of_y(row.get(zone, ""))
    return v


def load_csvs(store: StatsStore, csv_dir: str | Path) -> LoadReport:
    """Rebuild the historical tables from the CSV files in ``csv_dir``."""
    csv_dir = Path(csv_dir)
    report = LoadReport()
    events = _read(csv_dir / "ufc_event_details.csv")
    results = _read(csv_dir / "ufc_fight_results.csv")
    stats = _read(csv_dir / "ufc_fight_stats.csv")
    tott = _read(csv_dir / "ufc_fighter_tott.csv")
    resolver = FighterResolver(tott, report)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    event_rows, event_ids = [], {}
    for e in events:
        d = parse_date(e["DATE"])
        eid = id_from_url(e["URL"])
        event_ids[e["EVENT"]] = (eid, d.isoformat() if d else None)
        event_rows.append((eid, e["EVENT"], d.isoformat() if d else None, e["LOCATION"], 1, now))

    # Fallback for renamed events ("UFC Fight Night: X vs. Y" -> "Noche UFC: X vs. Y"):
    # match on the part after the colon when only one event has it.
    by_suffix: dict[str, list[str]] = defaultdict(list)
    for name in event_ids:
        by_suffix[name.split(":", 1)[-1].strip()].append(name)

    def event_for(name: str) -> str | None:
        if name in event_ids:
            return name
        same = by_suffix.get(name.split(":", 1)[-1].strip(), [])
        return same[0] if len(same) == 1 else None

    fight_rows, bout_key_to_fight = [], {}
    order_in_event: dict[str, int] = defaultdict(int)
    seen_fights: set[str] = set()
    for r in results:
        event_name = event_for(r["EVENT"])
        if event_name is None or " vs. " not in r["BOUT"]:
            report.skipped.append(f"{r['EVENT']} | {r['BOUT']}: event or bout name not recognised")
            continue
        fight_id = id_from_url(r["URL"])
        if fight_id in seen_fights:
            report.duplicates += 1
            continue
        seen_fights.add(fight_id)
        eid, date = event_ids[event_name]
        n1, n2 = (x.strip() for x in r["BOUT"].split(" vs. ", 1))
        wc, title, women = split_weight_class(r["WEIGHTCLASS"])
        f1, f2 = resolver.resolve(n1, wc), resolver.resolve(n2, wc)
        s1, s2 = (r["OUTCOME"].split("/") + ["", ""])[:2]
        outcome = "WIN" if {s1, s2} == {"W", "L"} else ("DRAW" if s1 == "D" else "NC")
        winner = f1 if s1 == "W" else (f2 if s2 == "W" else None)
        end_round = int(r["ROUND"]) if r["ROUND"].isdigit() else None
        end_time = parse_clock(r["TIME"])
        scheduled, lengths = parse_time_format(r["TIME FORMAT"])
        method = r["METHOD"]
        order = order_in_event[eid]
        order_in_event[eid] += 1
        fight_rows.append((
            fight_id, eid, date, order, f1, f2, s1, s2, winner, outcome, r["WEIGHTCLASS"], wc,
            int(title), int(women), method, method_group(method), r["DETAILS"], end_round, end_time,
            r["TIME FORMAT"], scheduled, fight_duration(end_round, end_time, lengths), r["REFEREE"],
        ))
        bout_key_to_fight[(r["EVENT"], r["BOUT"])] = (fight_id, {n1: f1, n2: f2})

    # Round rows, plus whole-fight totals (round 0) summed from the rounds.
    totals: dict[tuple[str, str], dict[str, int | None]] = {}
    stat_rows = []
    for s in stats:
        key = (s.get("EVENT", ""), s.get("BOUT", ""))
        if key not in bout_key_to_fight or not s.get("FIGHTER") or not s.get("ROUND", "").startswith("Round"):
            continue
        fight_id, name_map = bout_key_to_fight[key]
        fid = name_map.get(s["FIGHTER"])
        if fid is None:
            continue
        rnd = int(s["ROUND"].split()[-1])
        line = _stat_line(s)
        stat_rows.append((fight_id, fid, rnd, *[line[c] for c in STAT_COLUMNS]))
        tot = totals.setdefault((fight_id, fid), {c: None for c in STAT_COLUMNS})
        for c in STAT_COLUMNS:
            if line[c] is not None:
                tot[c] = (tot[c] or 0) + line[c]
    for (fight_id, fid), tot in totals.items():
        stat_rows.append((fight_id, fid, 0, *[tot[c] for c in STAT_COLUMNS]))

    fighter_rows = []
    for row in tott:
        dob = parse_date(row.get("DOB", ""))
        stance = row.get("STANCE") or None
        fighter_rows.append((id_from_url(row["URL"]), row["FIGHTER"], parse_height_cm(row.get("HEIGHT", "")),
                             parse_reach_cm(row.get("REACH", "")), stance if stance != "--" else None,
                             dob.isoformat() if dob else None, now))
    for name in report.placeholders:
        fighter_rows.append((f"name:{normalise_name(name)}", name, None, None, None, None, now))

    store.replace_history(event_rows, fight_rows, stat_rows, fighter_rows)
    report.events, report.fights = len(event_rows), len(fight_rows)
    report.stat_rows, report.fighters = len(stat_rows), len(fighter_rows)
    report.newest_event = max((e[2] for e in event_rows if e[2]), default=None)
    return report
