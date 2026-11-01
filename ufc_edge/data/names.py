"""Fighter-name normalisation for matching across sources.

UFCStats, The Odds API and the Kaggle dataset spell names differently
(accents, hyphens, "Jr.", nicknames such as Joe/Joseph, middle names,
family-name-first order). Matching uses normalised keys, plus a cautious
fuzzy fallback that only accepts a *unique* candidate with the same surname.
Anything it can't match is reported rather than guessed.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

# Common English short forms -> canonical first name.
_NICKNAMES = {
    "joe": "joseph", "joey": "joseph", "zach": "zachary", "zack": "zachary", "mike": "michael",
    "mikey": "michael", "alex": "alexander", "nick": "nicholas", "nico": "nicholas", "rob": "robert",
    "bob": "robert", "bobby": "robert", "robbie": "robert", "dan": "daniel", "danny": "daniel",
    "matt": "matthew", "chris": "christopher", "tom": "thomas", "tommy": "thomas", "will": "william",
    "bill": "william", "billy": "william", "tony": "anthony", "jim": "james", "jimmy": "james",
    "ben": "benjamin", "benny": "benjamin", "sam": "samuel", "josh": "joshua", "jon": "jonathan",
    "johnny": "john", "dave": "david", "steve": "steven", "stephen": "steven", "ed": "edward",
    "eddie": "edward", "andy": "andrew", "drew": "andrew", "cam": "cameron", "greg": "gregory",
    "jake": "jacob", "nate": "nathan", "pat": "patrick", "rick": "richard", "ricky": "richard",
    "rich": "richard", "ron": "ronald", "ken": "kenneth", "kenny": "kenneth", "tim": "timothy",
    "jeff": "jeffrey", "fred": "frederick", "freddy": "frederick", "vince": "vincent",
    "manny": "manuel", "abe": "abraham", "max": "maxim", "kate": "katherine", "katie": "katherine",
    "liz": "elizabeth", "beth": "elizabeth", "tina": "christina",
}


def normalise_name(name: str) -> str:
    """'José Aldo Jr.' -> 'jose aldo'; "Ji Yeon Kim" -> 'ji yeon kim'."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z\s]", " ", text.replace("'", "").replace("-", " ")).lower()
    parts = [p for p in text.split() if p not in _SUFFIXES]
    return " ".join(parts)


def canonical_first(first: str) -> str:
    return _NICKNAMES.get(first, first)


def name_keys(name: str) -> set[str]:
    """Every key a name can match on."""
    n = normalise_name(name)
    parts = n.split()
    keys = {n, "".join(parts), " ".join(sorted(parts))}  # sorted: "Weili Zhang" = "Zhang Weili"
    if len(parts) > 2:
        keys.add(f"{parts[0]} {parts[-1]}")
    if len(parts) >= 2:
        keys.add(f"{canonical_first(parts[0])} {' '.join(parts[1:])}")  # Joe Pyfer = Joseph Pyfer
    return keys


def same_fighter(a: str, b: str) -> bool:
    return bool(name_keys(a) & name_keys(b))


def _first_names_compatible(a: str, b: str) -> bool:
    a, b = canonical_first(a), canonical_first(b)
    if a == b:
        return True
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def fuzzy_unique_match(name: str, candidates: Iterable[tuple[str, str]]) -> str | None:
    """Return the id of the single candidate with the same surname and a compatible first name.

    "Jeongyoung Lee" -> "JeongYeong Lee", "Rafael Cerquiera" -> "Rafael Cerqueira".
    Returns None when zero or several candidates fit, so it never guesses between people.
    """
    parts = normalise_name(name).split()
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    hits = set()
    for cid, cname in candidates:
        cp = normalise_name(cname).split()
        if len(cp) < 2:
            continue
        surname_ok = cp[-1] == last or (
            len(last) >= 5 and difflib.SequenceMatcher(None, cp[-1], last).ratio() >= 0.85 and cp[0] == first)
        if surname_ok and _first_names_compatible(first, cp[0]):
            hits.add(cid)
    return hits.pop() if len(hits) == 1 else None
