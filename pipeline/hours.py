"""Parse free-text weekly hours from city datasets into {day: (open, close)} in 24h HH:MM.

Handles the formats in the Chicago warming/cooling-center and library data, e.g.
    "M-F 9am-8pm; Sat & Sun 10am-4pm, closed 1/19"
    "M, W: 12PM-8PM; TU, TH: 10AM-6PM; F, SA: 9AM-5PM; SU: 1PM-5PM"
    "Mon. & Wed., 10-6; Tues. & Thurs., Noon-8; Fri. & Sat., 9-5; Sun., 1-5"
    "M-F: 8:30 a.m. to 4:30 p.m."
One-off dates ("open 1/19 10am-3pm", "Holiday Closures: 5/25/26") are dropped: everything from the
first m/d date onward is ignored. Anything unparseable yields {} (hours unknown), never a guess.
"""
from __future__ import annotations

import re

from careplan import DAYS

_DAY = (r"(?:mon(?:day)?|tues?(?:day)?|tu|wed(?:nesday)?|thu(?:rs?(?:day)?)?|th|fri(?:day)?|sat(?:urday)?|sa|"
        r"sun(?:day)?|su|m|t|w|f)\.?")
_DAY_RE = re.compile(rf"\b({_DAY})(?![a-z])", re.I)
_T = r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?\s?m\.?|p\.?\s?m\.?)?|noon)"
_RANGE_RE = re.compile(rf"({_T})\s*(?:-|–|to)\s*({_T})", re.I)
_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}\b")
_ALIASES = {"m": "Mon", "mon": "Mon", "monday": "Mon", "t": "Tue", "tu": "Tue", "tue": "Tue", "tues": "Tue",
            "tuesday": "Tue", "w": "Wed", "wed": "Wed", "wednesday": "Wed", "th": "Thu", "thu": "Thu",
            "thur": "Thu", "thurs": "Thu", "thursday": "Thu", "f": "Fri", "fri": "Fri", "friday": "Fri",
            "sa": "Sat", "sat": "Sat", "saturday": "Sat", "su": "Sun", "sun": "Sun", "sunday": "Sun"}


def _clock(tok: str) -> tuple[int, int, str | None]:
    tok = tok.strip().lower().replace(" ", "").replace(".", "")
    if tok == "noon":
        return 12, 0, "pm"
    m = re.match(r"(\d{1,2})(?::(\d{2}))?(am|pm)?$", tok)
    return int(m.group(1)), int(m.group(2) or 0), m.group(3)


def _to24(h: int, ampm: str | None) -> int:
    if ampm == "am":
        return 0 if h == 12 else h
    if ampm == "pm":
        return h if h == 12 else h + 12
    return h


def _range(a: str, b: str) -> tuple[str, str] | None:
    (h1, m1, p1), (h2, m2, p2) = _clock(a), _clock(b)
    if p1 is None and p2 is not None:          # "1-5pm" -> pm; "10-6pm" -> am start
        p1 = p2 if (p2 == "pm" and h1 < h2 and h1 != 12) else ("am" if p2 == "pm" and h1 != 12 else p2)
    s = _to24(h1, p1) if p1 else (h1 + 12 if 1 <= h1 <= 6 else h1)
    e = _to24(h2, p2) if p2 else h2
    if p2 is None and e <= s and e < 12:
        e += 12
    if not (0 <= s < 24 and 0 < e <= 24 and (e, m2) > (s, m1)):
        return None
    return f"{s:02d}:{m1:02d}", f"{e:02d}:{m2:02d}"


def _days(text: str) -> list[str]:
    text = re.split(r"closed", text, flags=re.I)[-1]          # "Sun Closed; M-F ..." -> only "M-F"
    toks = [(m.start(), m.end(), _ALIASES[m.group(1).lower().rstrip(".")]) for m in _DAY_RE.finditer(text)]
    out: list[str] = []
    for i, (_, end, day) in enumerate(toks):
        if i and re.fullmatch(r"\s*(-|–|to)\s*", text[toks[i - 1][1]:toks[i][0]]):
            a, b = DAYS.index(out[-1]), DAYS.index(day)
            out += [DAYS[j] for j in range(a + 1, b + 1)] if a < b else []
        elif day not in out:
            out.append(day)
    return out


def parse_hours(text: str | None) -> dict[str, tuple[str, str]]:
    if not text or re.search(r"until further notice", text, re.I):
        return {}
    d = _DATE_RE.search(text)
    text = text[:d.start()] if d else text
    out: dict[str, tuple[str, str]] = {}
    prev = 0
    for m in _RANGE_RE.finditer(text):
        days, rng = _days(text[prev:m.start()]), _range(m.group(1), m.group(2))
        prev = m.end()
        if rng:
            for day in days:
                out.setdefault(day, rng)
    return {d: out[d] for d in DAYS if d in out}


def format_schedule(s: dict[str, tuple[str, str]]) -> str:
    """The inverse of careplan.data.read_schedule."""
    return ";".join(f"{d} {o}-{c}" for d, (o, c) in s.items())
