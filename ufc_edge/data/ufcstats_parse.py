"""Parsers for UFCStats.com pages.

These are pure functions (HTML in, dataclasses out) with no network access,
so they can be tested against saved pages.

Leakage warning
---------------
UFCStats lists the **winner first** on event and fight pages. If fighter1 or
fighter2 order ever reached a model, it would "learn" that fighter1 usually
wins. The stats database keeps the site's order for traceability, and the
feature builder randomises the orientation with a fixed seed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from ..errors import UfcEdgeError

BASE = "http://ufcstats.com"
COMPLETED_EVENTS_URL = f"{BASE}/statistics/events/completed?page=all"
UPCOMING_EVENTS_URL = f"{BASE}/statistics/events/upcoming?page=all"


class ParseError(UfcEdgeError):
    """A page did not have the structure the parser expects."""


# --------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class EventStub:
    event_id: str
    url: str
    name: str
    date: date | None
    location: str


@dataclass(frozen=True)
class BoutStub:
    """One row of an event's fight table. Works for completed and upcoming events."""

    bout_order: int  # 0 = main event (top of the card as listed)
    fight_id: str | None
    fight_url: str | None
    fighter1_id: str | None
    fighter1_name: str
    fighter2_id: str | None
    fighter2_name: str
    weight_class: str


@dataclass(frozen=True)
class EventPage:
    event_id: str
    name: str
    date: date | None
    location: str
    bouts: list[BoutStub]


@dataclass
class StatLine:
    """One fighter's numbers for a whole fight (round 0) or for a single round."""

    kd: int | None = None
    sig_landed: int | None = None
    sig_att: int | None = None
    total_landed: int | None = None
    total_att: int | None = None
    td_landed: int | None = None
    td_att: int | None = None
    sub_att: int | None = None
    rev: int | None = None
    ctrl_sec: int | None = None
    head_landed: int | None = None
    head_att: int | None = None
    body_landed: int | None = None
    body_att: int | None = None
    leg_landed: int | None = None
    leg_att: int | None = None
    distance_landed: int | None = None
    distance_att: int | None = None
    clinch_landed: int | None = None
    clinch_att: int | None = None
    ground_landed: int | None = None
    ground_att: int | None = None


@dataclass
class FightPage:
    fight_id: str
    fighter1_id: str
    fighter1_name: str
    fighter1_status: str  # W, L, D, NC
    fighter2_id: str
    fighter2_name: str
    fighter2_status: str
    bout_type: str           # raw, e.g. "UFC Lightweight Title Bout"
    weight_class: str        # e.g. "Lightweight", "Women's Strawweight", "Catch Weight"
    title_bout: bool
    women: bool
    method: str              # raw, e.g. "Decision - Unanimous"
    method_group: str        # KO_TKO, SUB, DEC, DQ, OTHER
    method_detail: str
    end_round: int | None
    end_time_sec: int | None
    time_format: str
    scheduled_rounds: int | None
    fight_duration_sec: int | None
    referee: str
    # stats[fighter_id][round] -> StatLine ; round 0 = whole fight
    stats: dict[str, dict[int, StatLine]] = field(default_factory=dict)

    @property
    def winner_id(self) -> str | None:
        if self.fighter1_status == "W":
            return self.fighter1_id
        if self.fighter2_status == "W":
            return self.fighter2_id
        return None

    @property
    def outcome(self) -> str:
        if self.winner_id:
            return "WIN"
        statuses = {self.fighter1_status, self.fighter2_status}
        return "DRAW" if statuses == {"D"} else "NC"


@dataclass(frozen=True)
class FighterPage:
    fighter_id: str
    name: str
    height_cm: float | None
    reach_cm: float | None
    stance: str | None
    dob: date | None


# ------------------------------------------------------------------- helpers
def id_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_date(value: str) -> date | None:
    value = value.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_clock(value: str) -> int | None:
    """'4:59' -> 299 seconds; '--' or blank -> None."""
    m = re.fullmatch(r"\s*(\d+):(\d{2})\s*", value or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def parse_x_of_y(value: str) -> tuple[int | None, int | None]:
    m = re.fullmatch(r"\s*(\d+)\s+of\s+(\d+)\s*", value or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def parse_int(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def parse_height_cm(value: str) -> float | None:
    m = re.search(r"(\d+)'\s*(\d+)\"", value or "")
    return round((int(m.group(1)) * 12 + int(m.group(2))) * 2.54, 1) if m else None


def parse_reach_cm(value: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\"", value or "")
    return round(float(m.group(1)) * 2.54, 1) if m else None


def method_group(method: str) -> str:
    m = method.lower()
    if "ko/tko" in m or "doctor" in m:
        return "KO_TKO"
    if "submission" in m:
        return "SUB"
    if "decision" in m:
        return "DEC"
    if m.strip() == "dq":
        return "DQ"
    return "OTHER"  # overturned, could not continue, other


def parse_time_format(fmt: str) -> tuple[int | None, list[int]]:
    """'3 Rnd (5-5-5)' -> (3, [300, 300, 300]). Unknown formats -> (None, [])."""
    m = re.search(r"(\d+)\s*Rnd[^(]*\(([\d\-]+)\)", fmt or "")
    if not m:
        return None, []
    lengths = [int(x) * 60 for x in m.group(2).split("-") if x]
    return int(m.group(1)), lengths


def fight_duration(end_round: int | None, end_time: int | None, lengths: list[int]) -> int | None:
    if end_round is None or end_time is None or not lengths or end_round > len(lengths):
        return None
    return sum(lengths[: end_round - 1]) + end_time


def split_weight_class(bout_type: str) -> tuple[str, bool, bool]:
    """'UFC Women's Flyweight Title Bout' -> ("Women's Flyweight", True, True)."""
    title = "title" in bout_type.lower()
    women = "women" in bout_type.lower()
    wc = re.sub(r"\b(UFC|Interim|Title|Bout|Tournament|Ultimate Fighter.*?Finale)\b", "", bout_type, flags=re.I)
    wc = re.sub(r"\b\d+\b", "", wc)
    wc = " ".join(wc.split())
    return wc or bout_type, title, women


# ------------------------------------------------------------------- parsers
def parse_event_list(html: str) -> list[EventStub]:
    soup = _soup(html)
    events: list[EventStub] = []
    for row in soup.select("tr.b-statistics__table-row"):
        link = row.select_one("a.b-link")
        if link is None or not link.get("href"):
            continue
        url = str(link["href"]).strip()
        tds = row.find_all("td")
        date_span = row.select_one("span.b-statistics__date")
        events.append(
            EventStub(
                event_id=id_from_url(url),
                url=url,
                name=_text(link),
                date=parse_date(_text(date_span)) if date_span else None,
                location=_text(tds[1]) if len(tds) > 1 else "",
            )
        )
    if not events:
        raise ParseError("No events found on the event list page.")
    return events


def _labelled_items(soup: BeautifulSoup) -> dict[str, str]:
    """Read 'Label: value' list items into a dict with lowercase keys."""
    items: dict[str, str] = {}
    for li in soup.select("li.b-list__box-list-item"):
        text = _text(li)
        if ":" in text:
            k, v = text.split(":", 1)
            items.setdefault(k.strip().lower(), v.strip())
    return items


def parse_event(html: str, event_id: str) -> EventPage:
    soup = _soup(html)
    name = _text(soup.select_one("span.b-content__title-highlight"))
    if not name:
        raise ParseError(f"Event {event_id}: no title found.")
    info = _labelled_items(soup)
    bouts: list[BoutStub] = []
    rows = [r for r in soup.select("tr.b-fight-details__table-row") if r.find("td")]
    for order, row in enumerate(rows):
        tds = row.find_all("td")
        if len(tds) < 7:
            continue
        link = row.get("data-link")
        fighter_links = tds[1].find_all("a")
        names = [_text(p) for p in tds[1].find_all("p")] or [_text(a) for a in fighter_links]
        if len(names) < 2:
            continue
        ids = [id_from_url(str(a["href"])) if a.get("href") else None for a in fighter_links[:2]]
        ids += [None] * (2 - len(ids))
        bouts.append(
            BoutStub(
                bout_order=order,
                fight_id=id_from_url(str(link)) if link else None,
                fight_url=str(link).strip() if link else None,
                fighter1_id=ids[0],
                fighter1_name=names[0],
                fighter2_id=ids[1],
                fighter2_name=names[1],
                weight_class=_text(tds[6]),
            )
        )
    return EventPage(
        event_id=event_id,
        name=name,
        date=parse_date(info.get("date", "")),
        location=info.get("location", ""),
        bouts=bouts,
    )


_TOTALS_COLS = 10  # Fighter, KD, Sig. str., Sig. str. %, Total str., Td, Td %, Sub. att, Rev., Ctrl
_SIG_COLS = 9      # Fighter, Sig. str, Sig. str. %, Head, Body, Leg, Distance, Clinch, Ground


def _cell_pair(td: Tag) -> list[str]:
    ps = td.find_all("p")
    vals = [_text(p) for p in ps] if ps else [_text(td)]
    return (vals + ["", ""])[:2]


def _stat_rows(soup: BeautifulSoup) -> tuple[list[list[Tag]], list[list[Tag]]]:
    """Find stat rows by shape: each has two fighter links in the first cell.

    Totals rows have 10 cells and significant-strike rows have 9. The first
    row of each kind is the whole fight, and the following rows are rounds
    1..n, in page order.
    """
    totals: list[list[Tag]] = []
    sig: list[list[Tag]] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if not tds or len(tds[0].find_all("a")) < 2:
            continue
        if len(tds) == _TOTALS_COLS:
            totals.append(tds)
        elif len(tds) == _SIG_COLS:
            sig.append(tds)
    return totals, sig


def _apply_totals(tds: list[Tag], lines: tuple[StatLine, StatLine]) -> None:
    for i, line in enumerate(lines):
        col = lambda n: _cell_pair(tds[n])[i]  # noqa: E731
        line.kd = parse_int(col(1))
        line.sig_landed, line.sig_att = parse_x_of_y(col(2))
        line.total_landed, line.total_att = parse_x_of_y(col(4))
        line.td_landed, line.td_att = parse_x_of_y(col(5))
        line.sub_att = parse_int(col(7))
        line.rev = parse_int(col(8))
        line.ctrl_sec = parse_clock(col(9))


def _apply_sig(tds: list[Tag], lines: tuple[StatLine, StatLine]) -> None:
    for i, line in enumerate(lines):
        col = lambda n: _cell_pair(tds[n])[i]  # noqa: E731
        line.head_landed, line.head_att = parse_x_of_y(col(3))
        line.body_landed, line.body_att = parse_x_of_y(col(4))
        line.leg_landed, line.leg_att = parse_x_of_y(col(5))
        line.distance_landed, line.distance_att = parse_x_of_y(col(6))
        line.clinch_landed, line.clinch_att = parse_x_of_y(col(7))
        line.ground_landed, line.ground_att = parse_x_of_y(col(8))


def parse_fight(html: str, fight_id: str) -> FightPage:
    soup = _soup(html)
    persons = soup.select("div.b-fight-details__person")
    if len(persons) != 2:
        raise ParseError(f"Fight {fight_id}: expected 2 fighters, found {len(persons)}.")
    people = []
    for p in persons:
        status = _text(p.select_one("i.b-fight-details__person-status")).upper()
        a = p.select_one("h3 a") or p.find("a")
        if a is None or not a.get("href"):
            raise ParseError(f"Fight {fight_id}: fighter link missing.")
        people.append((id_from_url(str(a["href"])), _text(a), status))

    bout_type = _text(soup.select_one("i.b-fight-details__fight-title"))
    weight_class, title, women = split_weight_class(bout_type)

    texts = soup.select("p.b-fight-details__text")
    details_line = _text(texts[0]) if texts else ""

    def field_after(label: str, stop_labels: list[str]) -> str:
        pattern = rf"{label}:\s*(.*?)\s*(?:(?:{'|'.join(stop_labels)}):|$)"
        m = re.search(pattern, details_line, flags=re.I)
        return m.group(1).strip() if m else ""

    labels = ["Method", "Round", "Time format", "Time", "Referee", "Details"]
    method = field_after("Method", labels[1:])
    end_round = parse_int(field_after("Round", ["Time format", "Time", "Referee"]))
    end_time = parse_clock(field_after(r"Time(?! format)", ["Time format", "Referee"]))
    time_format = field_after("Time format", ["Referee"])
    referee = field_after("Referee", ["Details"])
    detail = _text(texts[1]) if len(texts) > 1 else ""
    detail = re.sub(r"^Details:\s*", "", detail)
    scheduled, lengths = parse_time_format(time_format)

    fight = FightPage(
        fight_id=fight_id,
        fighter1_id=people[0][0], fighter1_name=people[0][1], fighter1_status=people[0][2],
        fighter2_id=people[1][0], fighter2_name=people[1][1], fighter2_status=people[1][2],
        bout_type=bout_type, weight_class=weight_class, title_bout=title, women=women,
        method=method, method_group=method_group(method), method_detail=detail,
        end_round=end_round, end_time_sec=end_time, time_format=time_format,
        scheduled_rounds=scheduled, fight_duration_sec=fight_duration(end_round, end_time, lengths),
        referee=referee,
    )

    totals, sig = _stat_rows(soup)
    ids = (fight.fighter1_id, fight.fighter2_id)
    for rnd, tds in enumerate(totals):
        order = _row_fighter_order(tds[0], ids)
        lines = (fight.stats.setdefault(order[0], {}).setdefault(rnd, StatLine()),
                 fight.stats.setdefault(order[1], {}).setdefault(rnd, StatLine()))
        _apply_totals(tds, lines)
    for rnd, tds in enumerate(sig):
        order = _row_fighter_order(tds[0], ids)
        lines = (fight.stats.setdefault(order[0], {}).setdefault(rnd, StatLine()),
                 fight.stats.setdefault(order[1], {}).setdefault(rnd, StatLine()))
        _apply_sig(tds, lines)
    return fight


def _row_fighter_order(first_td: Tag, ids: tuple[str, str]) -> tuple[str, str]:
    """Stat rows list both fighters. Map them to ids using the links, not the position."""
    row_ids = [id_from_url(str(a["href"])) for a in first_td.find_all("a") if a.get("href")][:2]
    if set(row_ids) == set(ids):
        return row_ids[0], row_ids[1]
    return ids


def parse_fighter(html: str, fighter_id: str) -> FighterPage:
    soup = _soup(html)
    name = _text(soup.select_one("span.b-content__title-highlight"))
    if not name:
        raise ParseError(f"Fighter {fighter_id}: no name found.")
    info = _labelled_items(soup)
    stance = info.get("stance", "").strip()
    return FighterPage(
        fighter_id=fighter_id,
        name=name,
        height_cm=parse_height_cm(info.get("height", "")),
        reach_cm=parse_reach_cm(info.get("reach", "")),
        stance=stance if stance and stance != "--" else None,
        dob=parse_date(info.get("dob", "")),
    )
