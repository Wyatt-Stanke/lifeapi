"""Spoken or typed commands on one item (`POST /extra/commands`): "set the due date to today
at 11:59 PM", "due at 11:59", "due oct 8 3 o'clock", "push it back a day", "reset the due
date", "note: bring a calculator", "done", "undo".

`interpret` turns a command into a `Plan`: the item fields to change and a sentence saying
what that does. It's forgiving: dates and times go through `when.parse(command=True)`, which
takes typos, spoken numbers and times without am/pm; what isn't said is kept from the item
("due at 5pm" keeps its date, "due friday" its time); and every guess is explained in
`Plan.notes`. Applying a plan and undoing it are in `storage` (the `actions` table) and the
API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import DONE_STATUSES
from ..scraper.dates import LOCAL_TZ
from .when import DEFAULT_TIME, normalize, parse

SOURCE_NAMES = {"google_classroom": "Google Classroom", "ap_classroom": "AP Classroom",
                "vhl": "VHL Central", "infinite_campus": "Infinite Campus", "albert": "Albert"}
EXAMPLES = ("“due friday 5pm”, “due at 11:59 pm”, “push it back a day”, “reset the due date”, "
            "“note: bring a calculator”, “done”, “not done” or “undo”")
UNDO, REDO = "undo", "redo"


class CommandError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Plan:
    # "due_at" (the student's deadline, UTC ISO; None: the source's), "note", "status" (a custom
    # item's own; on a scraped one the student's, `turned_in` or `assigned`, None: the source's)
    changes: dict[str, Any]
    summary: str
    notes: list[str] = field(default_factory=list)


def source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source.replace("_", " ").title())


def when_text(dt: datetime | None, now: datetime) -> str:
    """"today, Thu Oct 8 at 11:59 PM", "Fri Oct 16 at 8:00 AM", "Tue Jan 5, 2027 at …"."""
    if dt is None:
        return "no due date"
    dt = dt.astimezone(LOCAL_TZ)
    days = (dt.date() - now.date()).days
    rel = {0: "today, ", 1: "tomorrow, ", -1: "yesterday, "}.get(days, "")
    year = f", {dt.year}" if dt.year != now.year else ""
    return f"{rel}{dt:%a %b} {dt.day}{year} at {clock(dt.hour, dt.minute)}"


def clock(h: int, m: int) -> str:
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def local(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso).astimezone(LOCAL_TZ) if iso else None


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def quote(s: str, limit: int = 60) -> str:
    s = " ".join(s.split())
    return f"“{s[:limit - 1]}…”" if len(s) > limit else f"“{s}”"


# -- what a command says ------------------------------------------------------------------

_LEAD_RE = re.compile(r"^(?:(?:please|hey|ok(?:ay)?|so|um+|uh+|lifeapi|siri|can\s+you|could\s+you)[,\s]+)+", re.I)
_UNDO_RE = re.compile(r"(?:undo|undo\s+(?:that|it|this|the\s+last(?:\s+(?:one|change|command))?)|"
                      r"revert(?:\s+(?:that|it))?|never\s?mind|cancel(?:\s+that)?|take\s+(?:that|it)\s+back|oops)", re.I)
_REDO_RE = re.compile(r"redo(?:\s+(?:that|it|this))?|undo\s+(?:the\s+)?undo", re.I)
_NOTE_CLEAR_RE = re.compile(r"(?:clear|delete|remove|erase|reset|drop)\s+(?:the\s+|my\s+)?notes?|"
                            r"no\s+notes?|notes?\s+(?:clear|delete|none)", re.I)
_NOTE_RE = re.compile(r"^(?:(?P<add>add|append|also)\s+(?:(?:a|to\s+(?:the\s+|my\s+)?)\s*)?notes?"
                      r"|(?:set\s+(?:the\s+|my\s+)?)?notes?\b(?:\s+(?:to|is|that|says?)\b)?|memo\b)"
                      r"\s*[:\-–—,]?\s*(?P<text>\S.*)$", re.I | re.S)
_DONE_RE = re.compile(r"(?:mark(?:\s+(?:it|this|that))?(?:\s+as)?\s+|it'?s\s+|i'?m\s+|set\s+(?:it\s+)?(?:to\s+)?)?"
                      r"(?:done|finished|complete(?:d)?|turned\s+in|submitted|handed\s+in)"
                      r"|i\s+(?:did|finished|submitted|turned\s+in|handed\s+in)\s+it", re.I)
_NOT_DONE_RE = re.compile(r"(?:mark(?:\s+(?:it|this|that))?(?:\s+as)?\s+|it'?s\s+|set\s+(?:it\s+)?(?:to\s+)?)?"
                          r"(?:not\s+(?:yet\s+)?(?:done|finished|complete(?:d)?|turned\s+in|submitted)|undone|"
                          r"unfinished|incomplete|unsubmit(?:ted)?|reopen(?:\s+it)?|to\s*-?do|assigned)", re.I)
_DUE_WORDS = r"(?:due(?:\s+date)?|deadline|date)"
_RESET_RE = re.compile(
    rf"(?:reset|restore|revert|go\s+back\s+to|put\s+back|back\s+to|use)\s+(?:the\s+|its\s+|my\s+)?"
    rf"(?:(?:original|old|real|official|teacher'?s|classroom'?s|google\s+classroom'?s|platform'?s|source'?s)\s+)?"
    rf"{_DUE_WORDS}?|(?:original|old|official|real)\s+{_DUE_WORDS}|reset", re.I)
_REMOVE_RE = re.compile(rf"(?:remove|clear|delete|drop|no)\s+(?:the\s+)?{_DUE_WORDS}", re.I)
_AMOUNT_RE = re.compile(
    r"(?P<sign>[+-])?\s*(?P<n>\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|half\s+an?|half\s+a|a\s+couple(?:\s+of)?|couple(?:\s+of)?|a\s+few|few)\s*"
    r"(?P<unit>minutes?|mins?|hours?|hrs?|h|days?|d|weeks?|wks?|w)\b", re.I)
_LATER_RE = re.compile(r"\b(?:later|back|out|extend(?:ed)?|postpone[ds]?|delay(?:ed)?|push(?:ed)?|"
                       r"after|more|add)\b", re.I)
_EARLIER_RE = re.compile(r"\b(?:earlier|sooner|up|forward|ahead|before|bring|pull|less)\b", re.I)
_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


def interpret(command: str, item: dict[str, Any], now: datetime,
              usual: tuple[int, int] | None = None) -> Plan | str:
    """What `command` asks of `item` (an Item as the API returns it), at `now`. Returns a
    Plan, or UNDO/REDO for the API to resolve against the item's actions. `usual` is the
    course's usual due time, for a date said without one on an item that has no deadline.
    Raises CommandError (400: not understood, 409: not possible for this item)."""
    raw = _LEAD_RE.sub("", " ".join(command.split()).strip().rstrip(".!?")).strip()
    if not raw:
        raise CommandError(400, f"Say what to do, like {EXAMPLES}.")
    if _UNDO_RE.fullmatch(raw):
        return UNDO
    if _REDO_RE.fullmatch(raw):
        return REDO
    if _NOTE_CLEAR_RE.fullmatch(raw):
        return _note(item, None, append=False)
    if m := _NOTE_RE.match(raw):
        return _note(item, m.group("text").strip(), append=bool(m.group("add")))
    if _NOT_DONE_RE.fullmatch(raw):
        return status_plan(item, done=False)
    if _DONE_RE.fullmatch(raw):
        return status_plan(item, done=True)
    text = normalize(raw)
    if _REMOVE_RE.fullmatch(text) or _RESET_RE.fullmatch(text):
        return _reset_due(item, now, remove=bool(_REMOVE_RE.fullmatch(text)))
    if (shift := _shift(text)) is not None:
        return _shift_due(item, now, shift)
    if plan := _set_due(text, item, now, usual):
        if text.lower().split() != raw.lower().split():
            plan.notes.insert(0, f"Read it as {quote(text)}.")
        return plan
    raise CommandError(400, f"Couldn't understand {quote(raw)}. Try {EXAMPLES}.")


def _note(item: dict[str, Any], text: str | None, append: bool) -> Plan:
    old = (item.get("user") or {}).get("note")
    if text is None:
        if not old:
            return Plan({}, "There's no note to delete.")
        return Plan({"note": None}, f"Deleted the note (it said {quote(old)}).")
    if append and old:
        return Plan({"note": f"{old}\n{text}"}, f"Added to the note: {quote(text)}.")
    if old:
        return Plan({"note": text}, f"Note set to {quote(text)}, replacing {quote(old)}.",
                    ["Start with “add note” to add to it instead."])
    return Plan({"note": text}, f"Note set to {quote(text)}.")


def label(status: str | None) -> str:
    return (status or "no status").replace("_", " ")


def status_plan(item: dict[str, Any], done: bool) -> Plan:
    """Mark `item` finished or not. A custom item's status is its own (`done`/`assigned`). A
    scraped one's stays the platform's in `source_status`; the student's (`turned_in`/
    `assigned`) is kept only while it disagrees with that, so it's cleared when they agree."""
    if item.get("kind") in ("announcement", "material"):
        raise CommandError(409, f"{item['kind'].capitalize()}s aren't turned in.")
    if item.get("converted_from") is not None:
        status = "done" if done else "assigned"
        if item.get("status") == status:
            return Plan({}, "It's already marked done." if done else "It's already not done.")
        return Plan({"status": status}, "Marked done." if done else "Marked not done.")
    name = source_name(item["source"])
    theirs = item.get("source_status")
    mine = (item.get("user") or {}).get("status")
    if (theirs in DONE_STATUSES) == done:
        if mine is None:
            return Plan({}, f"{name} already has it as {label(theirs)}.")
        return Plan({"status": None}, f"Status back to {name}'s: {label(theirs)}.")
    status = "turned_in" if done else "assigned"
    if mine == status:
        return Plan({}, "It's already marked turned in." if done else "It's already marked not turned in.")
    return Plan({"status": status}, "Marked turned in." if done else "Marked not turned in.",
                [f"Only in lifeapi: {name} still has it as {label(theirs)}."])


def _reset_due(item: dict[str, Any], now: datetime, remove: bool) -> Plan:
    custom = item.get("converted_from") is not None
    current = local(item.get("due_at"))
    mine = (item.get("user") or {}).get("due_at")
    source = local(item.get("source_due_at"))
    name = source_name(item["source"])
    if not mine:
        if custom:
            return Plan({}, "It has no due date.")
        return Plan({}, f"The due date is already {name}'s: {when_text(source, now)}.")
    if custom:
        return Plan({"due_at": None}, f"Removed the due date (was {when_text(current, now)}).")
    notes = [f"{name}'s due date can't be removed, so it's back to that."] if remove else []
    return Plan({"due_at": None}, f"Due date back to {name}'s: {when_text(source, now)} "
                                  f"(was {when_text(current, now)}).", notes)


def _shift(text: str) -> timedelta | None:
    """"push it back a day" -> +1 day, "2 hours earlier" -> -2 hours, "+1d" -> +1 day. None
    unless the command moves the deadline by an amount ("due in 2 days" is a date)."""
    m = _AMOUNT_RE.search(text)
    if not m or re.search(r"\b(?:in|within)\s*$", text[:m.start()], re.I):
        return None
    if re.search(r"\bfrom\s+(?:now|today)\b", text[m.end():], re.I):
        return None
    rest = text[:m.start()] + " " + text[m.end():]
    if m.group("sign"):
        sign = -1 if m.group("sign") == "-" else 1
    elif _EARLIER_RE.search(rest):
        sign = -1
    elif _LATER_RE.search(rest):
        sign = 1
    else:
        return None
    raw = m.group("n").lower()
    if raw.startswith("half"):
        n = 0.5
    elif "couple" in raw:
        n = 2
    elif "few" in raw:
        n = 3
    else:
        n = float(raw) if raw[0].isdigit() else _NUMBERS[raw]
    unit = m.group("unit").lower()
    size = (timedelta(weeks=1) if unit.startswith("w") else timedelta(hours=1) if unit.startswith("h")
            else timedelta(minutes=1) if unit.startswith("m") else timedelta(days=1))
    return sign * n * size


def _amount(delta: timedelta) -> str:
    secs = abs(int(delta.total_seconds()))
    for size, name in ((604800, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if secs % size == 0 and secs >= size:
            n = secs // size
            return f"{n} {name}{'s' if n != 1 else ''}"
    return f"{secs // 60} minutes"


def _shift_due(item: dict[str, Any], now: datetime, delta: timedelta) -> Plan:
    current = local(item.get("due_at"))
    if current is None:
        raise CommandError(409, "It has no due date to move. Say when it's due instead, like "
                                "“due friday 5pm”.")
    new = current + delta
    word = "later" if delta > timedelta(0) else "earlier"
    return Plan({"due_at": _utc(new)}, f"Moved the due date {_amount(delta)} {word}, to "
                                       f"{when_text(new, now)} (was {when_text(current, now)}).",
                _past(new, now))


def _past(dt: datetime, now: datetime) -> list[str]:
    return ["That's in the past."] if dt < now else []


def _pick_half(hm: tuple[int, int], ref: tuple[int, int] | None) -> tuple[int, int]:
    """Morning or evening for a time said without am/pm ("at 5", "11:59"). Nothing is due
    between 1 and 6 in the morning, so those are afternoon; 12 is noon and :59 is a deadline
    at night. 7 to 11 is whichever is nearer the item's current due time (or the course's
    usual one), else the morning."""
    h, mi = hm
    if h == 12:
        return 12, mi
    am, pm = (h, mi), (h + 12, mi)
    if h <= 6 or mi == 59:
        return pm
    if ref:
        minutes = lambda c: c[0] * 60 + c[1]
        return am if abs(minutes(am) - minutes(ref)) < abs(minutes(pm) - minutes(ref)) else pm
    return am


def _set_due(text: str, item: dict[str, Any], now: datetime,
             usual: tuple[int, int] | None) -> Plan | None:
    specs = [s for s in parse(text, now, command=True)
             if not re.search(r"\b(?:from|was|not|instead\s+of)\s*$", text[:s.start], re.I)]
    if not specs:
        return None
    # "move it from friday to monday": the one after "to", else the last one said.
    spec = next((s for s in specs if re.search(r"\b(?:to|until|till|for)\s*$", text[:s.start], re.I)),
                specs[-1])
    current = local(item.get("due_at"))
    ref_hm = (current.hour, current.minute) if current else usual
    notes = []
    day = spec.day
    if day is None:
        day = current.date() if current else now.date()
        if current:
            notes.append(f"Kept its date, {current:%a %b} {current.day}.")
    if spec.hm is not None:
        hm = spec.hm
        if not spec.ampm:
            hm = _pick_half(hm, ref_hm)
            h, mi = spec.hm
            said = f"{h}:{mi:02d}" if mi else str(h)
            other = "am" if hm[0] >= 12 else "pm"
            notes.append(f"No am or pm said, so {clock(*hm)}; say “{said} {other}” for "
                         f"{'the morning' if other == 'am' else 'the evening'}.")
    elif spec.time_word:
        hm = DEFAULT_TIME
    elif current:
        hm = (current.hour, current.minute)
        notes.append(f"Kept its time, {clock(*hm)}.")
    else:
        hm = usual or DEFAULT_TIME
        notes.append(f"No time said, so {clock(*hm)}" + (", when this class's work is usually due."
                                                          if usual else "."))
    new = datetime.combine(day, datetime.min.time().replace(hour=hm[0], minute=hm[1]), tzinfo=LOCAL_TZ)
    if spec.day is None and current is None and new < now:
        new += timedelta(days=1)
    notes += _past(new, now)
    if current is not None and new == current:
        return Plan({}, f"It's already due {when_text(new, now)}.", notes)
    was = f" (was {when_text(current, now)})" if current else " (it had none)"
    return Plan({"due_at": _utc(new)}, f"Due date set to {when_text(new, now)}{was}.", notes)
