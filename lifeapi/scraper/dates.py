"""Parsing of the human-readable dates sites display ("Due Oct 9, 8:00 AM", "Tomorrow", "2:05 PM")."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for m in names
}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.I)
_MDY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b")
_NUMERIC_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def now() -> datetime:
    return datetime.now(LOCAL_TZ)


def parse_display_date(
    text: str | None,
    *,
    prefer: str = "nearest",
    posted: bool | None = None,
    reference: datetime | None = None,
) -> datetime | None:
    """Best-effort parse of a displayed date/time into an aware datetime (America/New_York).

    `prefer` resolves dates shown without a year: "past" (posted dates), "future",
    "nearest" (whichever year puts it closest to today), or "current_year" (for sites like
    Google Classroom that print the year whenever it isn't the current one).
    Date-only values get 23:59 (deadlines) or 00:00 (`posted=True`; defaults to
    prefer == "past").
    Returns None if nothing date-like is found.
    """
    if not text:
        return None
    ref = reference or now()
    if posted is None:
        posted = prefer == "past"
    s = text.strip()
    low = s.lower()

    tm = _TIME_RE.search(s)
    hour = minute = None
    if tm:
        hour = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "p" else 0)
        minute = int(tm.group(2) or 0)

    day = None
    explicit_year = False
    if "today" in low:
        day = ref.date()
    elif "tomorrow" in low:
        day = (ref + timedelta(days=1)).date()
    elif "yesterday" in low:
        day = (ref - timedelta(days=1)).date()
    else:
        for m in _MDY_RE.finditer(s):
            month = _MONTHS.get(m.group(1).lower())
            if not month:
                continue
            year = int(m.group(3)) if m.group(3) else ref.year
            explicit_year = bool(m.group(3))
            try:
                day = datetime(year, month, int(m.group(2))).date()
            except ValueError:
                continue
            break
        if day is None and (m := _NUMERIC_RE.search(s)):
            year = m.group(3)
            explicit_year = bool(year)
            year = int(year) + (2000 if year and len(year) == 2 else 0) if year else ref.year
            try:
                day = datetime(year, int(m.group(1)), int(m.group(2))).date()
            except ValueError:
                day = None
        if day is None:
            for i, name in enumerate(_WEEKDAYS):
                if re.search(rf"\b{name[:3]}(?:{name[3:]})?\b", low):
                    delta = (i - ref.weekday()) % 7
                    if prefer == "past":
                        delta = delta - 7 if delta else 0
                    day = (ref + timedelta(days=delta)).date()
                    break

    if day is None:
        if hour is None:
            return None
        day = ref.date()  # a bare time means today

    if hour is None:  # date only: end of day for deadlines, start of day for posts
        hour, minute = (0, 0) if posted else (23, 59)

    dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TZ)

    if prefer == "current_year":
        return dt
    if not explicit_year and not any(w in low for w in ("today", "tomorrow", "yesterday")):
        candidates = [dt.replace(year=dt.year + k) for k in (-1, 0, 1)]
        if prefer == "past":
            past = [c for c in candidates if c <= ref + timedelta(days=1)]
            dt = max(past) if past else dt
        elif prefer == "future":
            fut = [c for c in candidates if c >= ref - timedelta(days=1)]
            dt = min(fut) if fut else dt
        else:
            dt = min(candidates, key=lambda c: abs(c - ref))
    return dt
