"""Dates and times in free text, for announcement drafts (`drafts.py`) and spoken commands
(`commands.py`).

A rule-based temporal tagger: regexes find date parts ("Oct 8", "10/14", "this Friday",
"tomorrow", "end of next week", "in 3 days") and time parts ("8am", "11:59 p.m.", "noon",
"3 o'clock", "4 in the afternoon"), each is resolved against a reference time (when a post
was made, or now for a command), and a date and a time next to each other become one
`Spec`. It beat dateparser, parsedatetime, ctparse and Microsoft's Recognizers-Text on
teacher posts and commands: those read ordinary words ("sat", "may", "now", "in a second",
"We") and numbers ("1-5", "2.1", "3/4") as dates.

`parse(command=True)` is more forgiving, for short text that's known to be about a date:
lowercase "wed"/"sat"/"may" are dates, misspelt weekdays and months are corrected ("wensday"),
spoken numbers become digits ("eleven fifty nine pm"), "10-9" is a date, and a bare "at 5" or
"11:59" is a time whose half of the day is left for the caller to pick (`Spec.ampm`).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from ..scraper.dates import LOCAL_TZ

DEFAULT_TIME = (23, 59)  # a date-only deadline, as the scrapers store one

_WD = (r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:s|nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?"
       r"|sat(?:urday)?|sun(?:day)?)")
_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
        r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
# Abbreviations that are also ordinary words; in prose only taken as dates when capitalised.
_AMBIGUOUS = {"sat", "sun", "wed", "mon", "may", "mar"}
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12, "fourteen": 14}
_N = r"\d{1,2}|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fourteen"

_DATE_RES = [
    ("md", re.compile(rf"\b(?:(?P<wd>{_WD})\.?,?\s+)?(?P<mon>{_MON})\.?\s+(?P<day>\d{{1,2}})"
                      rf"(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>20\d\d)\b)?", re.I)),
    ("dm", re.compile(rf"\b(?:(?P<wd>{_WD})\.?,?\s+)?(?:the\s+)?(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
                      rf"\s+(?:of\s+)?(?P<mon>{_MON})\b\.?(?:,?\s+(?P<year>20\d\d)\b)?", re.I)),
    ("num", re.compile(rf"\b(?:(?P<wd>{_WD})\.?,?\s+\(?)?(?<![\d/.])(?P<mon>\d{{1,2}})/(?P<day>\d{{1,2}})"
                       rf"(?:/(?P<year>20\d\d|\d\d))?\b(?![/\d])(?!\s*(?:of|pages?|cups?)\b)", re.I)),
    ("ord", re.compile(r"\bthe\s+(?P<day>\d{1,2})(?:st|nd|rd|th)\b(?!\s+(?:period|grade|block|hour|"
                       r"question|problem|edition|chapter|step|paragraph|time|place)\b)", re.I)),
    ("wd", re.compile(rf"\b(?:(?P<mod>this\s+coming|this|next|coming|last|the\s+following|following)"
                      rf"\s+)?(?P<wd>{_WD})\b\.?(?:\s+(?P<week>next\s+week|this\s+week))?", re.I)),
    ("rel", re.compile(rf"""\b(?:
        (?P<dat>(?:the\s+)?day\s+after\s+tomorrow)
      | (?P<today>today|tonight|this\s+(?:morning|afternoon|evening))
      | (?P<tmrw>tomorrow|tomorow|tommorow|tommorrow|tmrw|tmr)
      | (?P<yest>yesterday)
      | (?P<eonw>(?:the\s+)?end\s+of\s+next\s+week)
      | (?P<eow>(?:the\s+)?end\s+of\s+(?:the|this)\s+week)
      | (?P<wkend>(?:this|the|over\s+the)\s+weekend)
      | (?P<nextwk>next\s+week)
      | (?P<inn>in\s+(?P<n>{_N})\s+(?P<unit>days?|weeks?|hours?|hrs?|minutes?|mins?))
      | (?P<from>(?P<n2>an?|one|two|three|\d)\s+weeks?\s+from\s+(?:today|now))
      | (?P<nextcls>(?:the\s+)?(?:(?:start|beginning)\s+of\s+)?(?:the\s+)?next\s+class
               (?:\s+(?:period|meeting|session))?|next\s+time\s+(?:we|our\s+class)\s+meets?)
      | (?P<eod>(?:the\s+)?end\s+of\s+(?:the\s+)?(?:day|today)|eod)
      | (?P<eom>(?:the\s+)?end\s+of\s+(?:the|this)\s+month)
    )\b""", re.I | re.X)),
]
# Commands only: "10-9" (in prose that's more often "pages 10-12").
_DASH_DATE_RE = re.compile(r"(?<![\d/.\-])\b(?P<mon>\d{1,2})-(?P<day>\d{1,2})(?:-(?P<year>20\d\d|\d\d))?\b(?![\d\-])")

TIME_RE = re.compile(r"""\b(?:
    (?P<h>\d{1,2})(?::(?P<mi>[0-5]\d))?\s*(?P<ap>[ap])\.?\s?m\b\.?
  | (?P<hc>\d{1,2})\.?(?P<mic>[0-5]\d)\s*(?P<apc>[ap])\.?\s?m\b\.?
  | (?P<oc>\d{1,2})\s*o['’]?\s*clock(?:\s*(?P<ocap>[ap])\.?\s?m\b\.?)?
  | (?P<pd>\d{1,2})(?::(?P<pdm>[0-5]\d))?\s+(?:in\s+the\s+(?P<part>morning|afternoon|evening)|at\s+(?P<night>night))
  | (?P<word>noon|midday|midnight)
  | (?P<h24>[01]?\d|2[0-3]):(?P<mi24>[0-5]\d)(?!\s*[ap]\.?\s?m\b)
)""", re.I | re.X)
# Commands only: "at 5", "by 9:30" with no am/pm.
_BARE_HOUR_RE = re.compile(r"\b(?:at|@|by|around|before)\s+(?P<h>\d{1,2})(?::(?P<mi>[0-5]\d))?\b"
                           r"(?![:/\d])(?!\s*(?:%|percent|points?|pts|pages?|problems?|questions?|days?|"
                           r"weeks?|hours?|hrs?|minutes?|mins?|[ap]\.?\s?m\b))", re.I)
# What may sit between a date and its time ("Friday at 8am", "8:00 AM on 10/14", "Fri (11:59pm)").
_JOIN_RE = re.compile(r"^[\s,()\-–—@]*(?:(?:at|by|before|around|until|till|on|no\s+later\s+than)"
                      r"[\s,()\-–—@]*)?$", re.I)
CLASS_TIME_RE = re.compile(r"\b(?:(?:start|beginning|end)\s+of\s+(?:the\s+)?(?:class|period|block)"
                           r"|before\s+class|in\s+class)\b", re.I)


@dataclass
class Spec:
    """A date and/or time as written, resolved as far as the text says."""

    start: int
    end: int
    day: date | None = None  # None: only a time was written
    hm: tuple[int, int] | None = None  # None: only a date was written
    ampm: bool = True  # False: "at 5", "11:59", where morning or evening is a guess
    time_word: bool = False  # "tonight", "end of the day": the time is in the words (23:59)
    explicit: bool = False  # a calendar date, as opposed to a weekday or "tomorrow"
    weak: bool = False  # a bare "3:00": in prose only taken next to a date
    vague: bool = False  # "next week": a day picked from a span
    past: bool = False  # "last Friday", "yesterday"
    text: str = ""


@dataclass
class Mention:
    """A date or time found in a post, resolved to a moment against when it was posted."""

    start: int
    end: int
    text: str
    at: datetime
    time: str  # where the time of day came from: "text", "course" or "default"
    has_date: bool = True
    vague: bool = False
    past: bool = False
    score: float = 0.0  # how much it reads like a deadline (drafts.py)

    def out(self) -> dict[str, Any]:
        return {"text": self.text, "start": self.start, "end": self.end,
                "at": self.at.isoformat(), "time": self.time}


def _wd_index(s: str) -> int:
    return WEEKDAYS.index(s[:3].lower())


def _month_index(s: str) -> int:
    return MONTHS.index(s[:3].lower()) + 1


def _year_for(month: int, day: int, ref: date) -> date | None:
    """The date nearest `ref` that `month`/`day` can mean: up to ~3 months before it (an old
    deadline someone mentions), otherwise the next one."""
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= ref - timedelta(days=90):
            return d
    return None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _next_school_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _date_part(kind: str, m: re.Match, ref: datetime, lenient: bool) -> Spec | None:
    g = m.groupdict()
    today = ref.date()
    p = Spec(m.start(), m.end(), hm=None)

    def ambiguous(word: str | None) -> bool:
        return not lenient and bool(word) and word.lower() in _AMBIGUOUS and word[0].islower()

    if kind in ("md", "dm", "num", "dash"):
        if ambiguous(g.get("wd")) or (kind in ("md", "dm") and ambiguous(g["mon"])):
            return None
        month = int(g["mon"]) if kind in ("num", "dash") else _month_index(g["mon"])
        day = int(g["day"])
        year = g.get("year")
        try:
            if year:
                p.day = date(int(year) + (2000 if len(year) == 2 else 0), month, day)
            else:
                p.day = _year_for(month, day, today)
        except ValueError:
            return None
        if p.day is None:
            return None
        p.explicit = True
    elif kind == "ord":
        day = int(g["day"])
        d = today.replace(day=1)
        for _ in range(3):  # this month's, else the next month that has it
            try:
                if (c := d.replace(day=day)) >= today:
                    p.day = c
                    break
            except ValueError:
                pass
            d = (d + timedelta(days=32)).replace(day=1)
        if p.day is None:
            return None
    elif kind == "wd":
        if ambiguous(g["wd"]):
            return None
        wd = _wd_index(g["wd"])
        mod = (g["mod"] or "").lower().split()
        week = (g["week"] or "").lower()
        ahead = (wd - today.weekday()) % 7
        if week.startswith("next") or mod == ["next"]:
            p.day = _monday(today) + timedelta(days=7 + wd)
        elif week.startswith("this"):
            p.day = _monday(today) + timedelta(days=wd)
        elif mod == ["last"]:
            p.day = today - timedelta(days=(today.weekday() - wd) % 7 or 7)
            p.past = True
        elif "following" in mod:
            p.day = today + timedelta(days=(ahead or 7) + 7)
        elif mod == ["this"]:
            p.day = today + timedelta(days=ahead)
        else:  # "Friday", "coming Friday": the next one (a week ahead if that's today)
            p.day = today + timedelta(days=ahead or 7)
    else:  # rel
        word = next(k for k in ("dat", "today", "tmrw", "yest", "eonw", "eow", "wkend", "nextwk",
                                "inn", "from", "nextcls", "eod", "eom") if g[k])
        if word == "dat":
            p.day = today + timedelta(days=2)
        elif word == "today":
            p.day = today
            p.time_word = g["today"].lower() == "tonight"
        elif word == "tmrw":
            p.day = today + timedelta(days=1)
        elif word == "yest":
            p.day, p.past = today - timedelta(days=1), True
        elif word == "eow":
            friday = _monday(today) + timedelta(days=4)
            p.day = friday if friday >= today else friday + timedelta(days=7)
        elif word == "eonw":
            p.day = _monday(today) + timedelta(days=11)
        elif word == "wkend":
            p.day = today + timedelta(days=(6 - today.weekday()) % 7)
        elif word == "nextwk":
            p.day, p.vague = _monday(today) + timedelta(days=7), True
        elif word in ("inn", "from"):
            raw = (g["n"] or g["n2"]).lower()
            n = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
            unit = "w" if word == "from" else g["unit"].lower()[0]
            if unit in "hm":  # "in 2 hours": a moment, not a day
                at = ref + (timedelta(hours=n) if unit == "h" else timedelta(minutes=n))
                p.day, p.hm = at.date(), (at.hour, at.minute)
            else:
                p.day = today + timedelta(days=n * (7 if unit == "w" else 1))
        elif word == "nextcls":
            p.day = _next_school_day(today)
        elif word == "eod":
            p.day, p.time_word = today, True
        else:  # eom
            p.day = (today.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return p


def _time_part(m: re.Match, bare: bool = False) -> Spec | None:
    g = m.groupdict()
    p = Spec(m.start(), m.end())
    if bare:  # "at 5"
        p.hm, p.ampm = (int(g["h"]), int(g["mi"] or 0)), False
    elif g.get("word"):
        p.hm = (12, 0) if g["word"].lower() in ("noon", "midday") else (23, 59)
    elif g.get("h") or g.get("hc"):
        h, mi, ap = (g["h"], g["mi"], g["ap"]) if g.get("h") else (g["hc"], g["mic"], g["apc"])
        p.hm = (int(h) % 12 + (12 if ap.lower() == "p" else 0), int(mi or 0))
    elif g.get("oc"):
        h = int(g["oc"])
        if g["ocap"]:
            p.hm = (h % 12 + (12 if g["ocap"].lower() == "p" else 0), 0)
        else:
            p.hm, p.ampm = (h, 0), False
    elif g.get("pd"):
        h = int(g["pd"]) % 12
        pm = g["night"] or g["part"].lower() in ("afternoon", "evening")
        p.hm = (h + (12 if pm else 0), int(g["pdm"] or 0))
    else:
        h = int(g["h24"])
        p.hm, p.weak, p.ampm = (h, int(g["mi24"])), True, h == 0 or h >= 13
    if not p.hm or p.hm[0] > 23 or (not p.ampm and not 1 <= p.hm[0] <= 12):
        return None
    return p


def _keep_longest(parts: list[Spec]) -> list[Spec]:
    kept: list[Spec] = []
    for p in sorted(parts, key=lambda p: (-(p.end - p.start), p.start)):
        if all(p.end <= k.start or p.start >= k.end for k in kept):
            kept.append(p)
    return sorted(kept, key=lambda p: p.start)


def parse(text: str, ref: datetime, command: bool = False) -> list[Spec]:
    """Every date and time in `text`, in order, resolved against `ref` as far as the text
    goes: a date without a time keeps `hm` None, a time without a date `day` None. In prose
    a bare "3:00" counts only next to a date; in a command (`command=True`, text known to be
    about a date) it always counts. Call `normalize()` first on a command."""
    dates = []
    for kind, rx in _DATE_RES + ([("dash", _DASH_DATE_RE)] if command else []):
        for m in rx.finditer(text):
            if p := _date_part(kind, m, ref, lenient=command):
                dates.append(p)
    dates = _keep_longest(dates)
    times = [p for m in TIME_RE.finditer(text) if (p := _time_part(m))]
    if command:
        times += [p for m in _BARE_HOUR_RE.finditer(text) if (p := _time_part(m, bare=True))]
        for t in times:  # "at 5": the time is the number, not the "at"
            if text[t.start:t.end].lower().startswith(("at", "by", "around", "before", "@")):
                t.start = re.search(r"\d", text[t.start:t.end]).start() + t.start
    times = _keep_longest(times)
    times = [t for t in times if all(t.end <= d.start or t.start >= d.end for d in dates)]

    # "Friday (10/14)": a weekday next to a calendar date is one date.
    merged: list[Spec] = []
    for d in dates:
        prev = merged[-1] if merged else None
        if prev and re.fullmatch(r"[\s,()]*", text[prev.end:d.start]) and prev.explicit != d.explicit:
            prev.day = prev.day if prev.explicit else d.day
            prev.explicit = True
            prev.end = d.end
            continue
        merged.append(d)

    out: list[Spec] = []
    used: set[int] = set()
    for d in merged:
        if d.hm is not None:  # "in 2 hours" carries its own time
            out.append(d)
            continue
        i = next((i for i, t in enumerate(times) if i not in used and (
            (0 <= t.start - d.end <= 20 and _JOIN_RE.match(text[d.end:t.start]))
            or (0 <= d.start - t.end <= 20 and _JOIN_RE.match(text[t.end:d.start])))), None)
        if i is not None:
            t = times[i]
            used.add(i)
            d.start, d.end = min(d.start, t.start), max(d.end, t.end)
            d.hm, d.ampm = t.hm, t.ampm
        out.append(d)
    for i, t in enumerate(times):
        if i in used or (t.weak and not command):
            continue
        line = (text.rfind("\n", 0, t.start), text.find("\n", t.end) % (len(text) + 1))
        same = [d for d in out if line[0] < d.start and d.end <= line[1] and d.hm is None
                and not d.time_word]
        if len(same) == 1 and not command:  # "- Fri: lab report due by 11:59pm"
            same[0].hm, same[0].ampm = t.hm, t.ampm
            continue
        out.append(t)
    for s in out:
        s.start, s.end = _trim(text, s.start, s.end)
        s.text = text[s.start:s.end]
    return sorted(out, key=lambda s: s.start)


def find_dates(text: str, ref: datetime, usual: tuple[int, int] | None = None) -> list[Mention]:
    """Every date and time in a post, resolved to moments against `ref` (when it was
    posted). A date written without a time gets `usual` (the course's usual due time), else
    23:59; a time without a date is the day it was posted, or the next if that's past."""
    out = []
    for s in parse(text, ref):
        if s.day is None:
            at = _at(ref.date(), s.hm if s.ampm else afternoon(s.hm))
            if at < ref:
                at += timedelta(days=1)
            out.append(Mention(s.start, s.end, s.text, at, "text", has_date=False))
            continue
        if s.hm is not None:
            hm, source = (s.hm if s.ampm else afternoon(s.hm)), "text"
        elif s.time_word:
            hm, source = DEFAULT_TIME, "text"
        elif usual:
            hm, source = usual, "course"
        else:
            hm, source = DEFAULT_TIME, "default"
        out.append(Mention(s.start, s.end, s.text, _at(s.day, hm), source, vague=s.vague, past=s.past))
    return out


def afternoon(hm: tuple[int, int]) -> tuple[int, int]:
    """A time given without am/pm, as school prose means it: 1-6 is the afternoon."""
    h, mi = hm
    return (h + 12 if 1 <= h <= 6 else h % 24, mi)


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """A span without stray punctuation: a closing parenthesis it opened is taken in, a
    sentence's full stop left out ("Friday." -> "Friday", but "8 a.m." stays)."""
    if text[start:end].count("(") > text[start:end].count(")") and text[end:end + 1] == ")":
        end += 1
    while start < end and text[start] in " ,":
        start += 1
    while end > start and text[end - 1] in " ,":
        end -= 1
    if text[end - 1:end] == "." and not re.search(r"[ap]\.m\.$", text[start:end], re.I):
        end -= 1
    if text[start:start + 1] == "(" and text[end - 1:end] == ")":
        start, end = start + 1, end - 1
    return start, end


def _at(d: date, hm: tuple[int, int]) -> datetime:
    return datetime.combine(d, time(*hm), tzinfo=LOCAL_TZ)


# -- commands -----------------------------------------------------------------------------

_FULL_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
               "january", "february", "march", "april", "june", "july", "august", "september",
               "october", "november", "december", "tomorrow", "tonight", "today"]
_KNOWN = set(_FULL_NAMES) | {"noon", "midnight", "morning", "afternoon", "evening", "minute",
                            "minutes", "hours", "weeks", "later", "earlier", "o'clock", "clock",
                            "date", "deadline", "back", "week", "month", "days", "note", "notes",
                            "done", "undo", "redo", "reset", "original", "change", "move", "push",
                            "extend", "postpone", "delay", "remove", "clear", "delete", "before",
                            "after", "until", "next", "this", "following", "coming", "weekend",
                            "the", "make", "sure", "mark", "set", "due", "class", "period"}
_SMALL = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
          "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_HOUR_WORD = r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_MINUTE_WORDS = (r"(?:oh[\s-]+(?:one|two|three|four|five|six|seven|eight|nine)|ten|eleven|twelve|thirteen|"
                 r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|(?:twenty|thirty|forty|fifty)"
                 r"(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?)")
_SPOKEN_TIME_RE = re.compile(
    rf"\b(?P<h>{_HOUR_WORD})(?:[\s-]+(?P<m>{_MINUTE_WORDS}))?(?=\s*(?:[ap]\.?\s?m\b|o['’]?\s*clock\b|"
    rf"in\s+the\s+(?:morning|afternoon|evening)|at\s+night))", re.I)
_SPOKEN_AT_RE = re.compile(rf"\b(?P<at>at|by)\s+(?P<h>{_HOUR_WORD})(?:[\s-]+(?P<m>{_MINUTE_WORDS}))?\b", re.I)


def _minutes(words: str | None) -> int:
    total = 0
    for w in re.split(r"[\s-]+", (words or "").lower()):
        total += _TENS.get(w, _SMALL.get(w, 0))
    return total


def normalize(text: str) -> str:
    """A spoken or typed command made easier to parse: curly quotes and stray punctuation
    gone, misspelt weekdays and months fixed, spoken times written as digits ("eleven fifty
    nine pm" -> "11:59 pm")."""
    s = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s).strip().rstrip(".!?")

    def fix(m: re.Match) -> str:
        w = m.group(0)
        low = w.lower()
        if len(low) < 5 or low in _KNOWN:
            return w
        close = difflib.get_close_matches(low, _FULL_NAMES, n=1, cutoff=0.8)
        return close[0] if close else w

    s = re.sub(r"[A-Za-z]+", fix, s)

    def spoken(m: re.Match) -> str:
        h = _SMALL[m.group("h").lower()]
        mi = _minutes(m.group("m"))
        prefix = m.group("at") + " " if "at" in m.groupdict() else ""
        return f"{prefix}{h}:{mi:02d}" if mi or m.group("m") else f"{prefix}{h}"

    s = _SPOKEN_TIME_RE.sub(spoken, s)
    s = _SPOKEN_AT_RE.sub(spoken, s)
    return s
