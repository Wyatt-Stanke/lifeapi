"""Drafts for turning an announcement into an assignment (`GET /extra/drafts/...`).

No model and no network, so it's instant and says why it suggests what it does. Four steps:

1. The temporal tagger (`when.py`) finds every date and time in the text ("this Friday at
   8am", "10/14", "tomorrow", "end of next week", "in 3 days") and resolves each against
   when the announcement was posted, not against today.
2. Each date is scored for how much it reads like a deadline from the words around it
   ("due", "by", "no later than", "moved to" score up; "last", "was due", a date before the
   post score down), and each sentence for how much it asks for work (assessment and work
   nouns, imperative verbs, "don't forget", a deadline; greetings and sign-offs score down).
3. The best sentences become drafts: the deadline and its cue words are cut out and the
   reminder phrasing is trimmed ("Reminder: your Chapter 5 vocab quiz is due Friday!" ->
   "Chapter 5 vocab quiz").
4. The course's own items, the student's data, fill the gaps: the time the teacher usually
   sets for a date given without one, how the teacher names recurring work ("Lab 4" makes
   "lab 5" read "Lab 5"), and which existing items the announcement may be about (a moved
   deadline, or work the platform already lists), by how much of each item's title the
   announcement contains, weighted by how rare each word is in the course.

Everything is a suggestion that a person checks in a form, so it errs towards filling
something in.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from ..scraper.dates import LOCAL_TZ
from .when import CLASS_TIME_RE, DEFAULT_TIME, MONTHS, TIME_RE, WEEKDAYS, Mention, find_dates

MAX_DRAFTS = 5
MAX_RELATED = 3
TITLE_MAX = 100

# -- vocabulary ---------------------------------------------------------------------------


_ASSESSMENT = (r"quiz(?:zes)?|tests?|exams?|midterms?|final\s+exams?|finals|assessments?|progress\s+checks?|psat"
               r"|retakes?|unit\s+tests?|frq|mcq|dbq|leq|saq")
_WORK = (r"homework|hw|assignments?|worksheets?|essays?|projects?|labs?|lab\s+reports?|reports?"
         r"|papers?|presentations?|readings?|chapters?|problem\s+sets?|psets?|packets?|notes"
         r"|outlines?|drafts?|cop(?:y|ies)|journals?|reflections?|study\s+guides?|flash\s?cards|vocab(?:ulary)?"
         r"|questions|exercises|problems|portfolios?|posters?|slides|edpuzzles?|delta\s?math|ixl"
         r"|khan\s+academy|quizlet|ap\s+classroom|practice|review|permission\s+slips?|forms?"
         r"|pages?|pgs?|p\.\s*\d|#\s*\d|annotations?|summary|summaries|responses?|discussion\s+posts?")
_VERBS = (r"complete|finish|submit|turn\s+in|hand\s+in|upload|read|write|study|prepare|bring"
          r"|watch|answer|do|practice|review|print|sign|return|fill\s+out|work\s+on|post|respond"
          r"|annotate|memorize|bring|email")
_ASSESSMENT_RE = re.compile(rf"\b(?:{_ASSESSMENT})\b", re.I)
_WORK_RE = re.compile(rf"\b(?:{_WORK})(?:\b|(?<=\d))", re.I)
_VERB_RE = re.compile(rf"\b(?:{_VERBS})\b", re.I)
_OBLIGATION_RE = re.compile(
    r"\b(?:please|make\s+sure|don'?t\s+forget|do\s+not\s+forget|remember\s+to|reminder|"
    r"you\s+(?:will\s+)?need\s+to|you\s+must|you\s+should|be\s+sure\s+to|due|deadline)\b", re.I)
_SOCIAL_RE = re.compile(
    r"^\W*(?:good\s+(?:morning|afternoon|evening)|hi|hello|hey|dear|greetings)\b|"
    r"\b(?:thank\s+you|thanks|have\s+a\s+(?:great|good|nice|wonderful)|enjoy\s+your|see\s+you|"
    r"let\s+me\s+know\s+if|if\s+you\s+have\s+(?:any\s+)?questions|feel\s+free|email\s+me\s+if|"
    r"reach\s+out|great\s+job|good\s+job|well\s+done|congrat\w*|proud\s+of)\b", re.I)
_EVENT_RE = re.compile(r"\b(?:no\s+(?:school|class)|class\s+(?:is\s+)?cancel+ed|half\s+day|"
                       r"early\s+dismissal|absent)\b", re.I)
_MOVED_RE = re.compile(r"\b(?:extend(?:ed|ing)?|extension|moved|pushed(?:\s+back)?|postponed|"
                       r"rescheduled|delayed|new\s+due\s+date|now\s+due|changed\s+the\s+due\s+date)\b", re.I)
_POINTS_RE = re.compile(r"\b(\d{1,4}(?:\.\d+)?)\s*-?\s*(?:points?|pts?)\b(?!\s*extra)", re.I)
_TOPIC_RE = re.compile(r"\b(unit|chapter|ch\.?|module|lesson|topic|section|part|week)\s*"
                       r"(\d+[a-z]?|[ivx]{1,4}\b)", re.I)

# -- sentences ----------------------------------------------------------------------------

_ABBREVIATIONS = {"p", "pg", "pgs", "pp", "ch", "chap", "no", "nos", "vol", "mr", "mrs", "ms",
                  "dr", "st", "eg", "ie", "etc", "vs", "approx", "a.m", "p.m", "am", "pm", "sec",
                  "fig", "ex", "q", "pt", "pts", "min", "hr", "hrs", "e.g", "i.e", "u.s"}
_ABBREVIATIONS |= set(MONTHS) | {"sept"} | set(WEEKDAYS) | {"tues", "thur", "thurs", "weds"}
_BOUNDARY_RE = re.compile(r"[.!?]+[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence: every line, split further at sentence punctuation
    that isn't an abbreviation ("p. 45", "Ch. 3", "Oct. 9")."""
    spans = []
    for line in re.finditer(r"[^\n]+", text):
        start = line.start()
        for b in _BOUNDARY_RE.finditer(line.group()):
            word = re.search(r"([\w.]+)$", line.group()[:b.start()])
            if word and word.group(1).lower().strip(".") in _ABBREVIATIONS:
                continue
            spans.append((start, line.start() + b.end()))
            start = line.start() + b.end()
        spans.append((start, line.end()))
    return [(a, b) for a, b in spans if text[a:b].strip()]


_CLAUSE_RE = re.compile(r"\s*(?:;|,?\s+and\s+(?=(?:the|a|an|your|our|then|also)\b)|,\s+(?:then|also)\s+"
                        r"|,?\s+(?:plus|also)\s+)", re.I)


def clauses(text: str, sents: list[tuple[int, int]], mentions: list[Mention]) -> list[tuple[int, int]]:
    """Sentences, with one that has several dates split between them where a clause starts
    ("lab 5 is due Friday and the Unit 2 quiz is Tuesday")."""
    out = []
    for a, b in sents:
        inside = [m for m in mentions if a <= m.start < b]
        start = a
        for left, right in zip(inside, inside[1:]):
            cut = next((c for c in _CLAUSE_RE.finditer(text, left.end, right.start)), None)
            if cut:
                out.append((start, cut.start()))
                start = cut.end()
        out.append((start, b))
    return out


def _deadline_score(m: Mention, text: str, sent: tuple[int, int], ref: datetime) -> float:
    before = text[max(sent[0], m.start - 40):m.start].lower()
    after = text[m.end:min(sent[1], m.end + 30)].lower()
    whole = text[sent[0]:sent[1]].lower()
    score = 0.0
    if re.search(r"\bdue\b[^.!?]{0,25}$", before) or re.match(r"\W{0,3}deadline\b", after):
        score += 3
    if re.search(r"\b(?:no\s+later\s+than|deadline(?:\s+is)?)\W*$", before):
        score += 3
    elif re.search(r"\bby\W*$", before):
        score += 2.5
    elif re.search(r"\b(?:before|until|till)\W*$", before):
        score += 1.5
    elif re.search(r"\b(?:on|for|is|are)\W*$", before):
        score += 0.5
    elif re.search(r"^\W*\w[^:.!?]{0,40}:\s*$", before):  # "Rough draft: Oct 31"
        score += 1.5
    if _MOVED_RE.search(whole):
        if re.search(r"\b(?:to|until|till|now)\W*$", before):
            score += 2.5
        elif re.search(r"\b(?:from|was|originally|previously|instead\s+of)\W*$", before):
            score -= 3
    elif re.search(r"\b(?:was|were)\s+due\b[^.!?]{0,15}$", before):
        score -= 2
    if re.search(r"\b(?:posted|assigned|started|began|learned|discussed)\b[^.!?]{0,20}$", before):
        score -= 1.5
    if _VERB_RE.search(whole):
        score += 1
    if _ASSESSMENT_RE.search(whole):
        score += 1.5
    if _EVENT_RE.search(whole):
        score -= 2
    if m.past or m.at.date() < ref.date():
        score -= 3
    if m.vague:
        score -= 1
    if m.time == "text":
        score += 0.5
    if not m.has_date:
        score -= 0.5
    return score


def _task_score(s: str, best_date: float | None) -> float:
    score = 0.0
    if best_date is not None and best_date > 0:
        score += 1  # its own deadline, not one borrowed from another sentence
        if re.search(r"\b(?:will\s+be|is|are)\s+(?:on|due)\b", s, re.I):
            score += 1
    if _ASSESSMENT_RE.search(s):
        score += 2
    if _WORK_RE.search(s):
        score += 1.5
    if _VERB_RE.search(s):
        score += 1.5
    if _OBLIGATION_RE.search(s):
        score += 1
    if best_date is not None and best_date > 0:
        score += min(best_date, 4)
    if _SOCIAL_RE.search(s):
        score -= 3
    if _EVENT_RE.search(s):
        score -= 2
    words = len(s.split())
    if words < 3:
        score -= 1
    elif words > 45:
        score -= 0.5
    return score


# -- titles -------------------------------------------------------------------------------

_LEADING = [re.compile(p, re.I) for p in (
    r"^(?:[\s\-–—*•▪◦·:;,.)\]]+|\d{1,2}[.)]\s+)",
    r"^(?:\d+(?:st|nd|rd|th)\s+)?(?:period|block)(?:\s+\d+)?\s*[,:\-–—]\s*",
    r"^(?:good\s+(?:morning|afternoon|evening)|hi|hello|hey|dear|greetings)\b[^,.!:\n]{0,30}[,.!:]\s*",
    r"^(?:(?:just\s+)?(?:a\s+)?(?:friendly\s+|quick\s+|gentle\s+)?reminders?|important|attention|"
    r"note|update|fyi|heads\s+up|announcement|ps|p\.s\.)\s*[:\-–—!,.]+\s*",
    r"^(?:(?:just\s+)?(?:a\s+)?(?:friendly\s+|quick\s+|gentle\s+)?reminders?(?:\s+that|\s+to|\s+about)?|"
    r"remember(?:\s+that|\s+to)?|don'?t\s+forget(?:\s+that|\s+to|\s+about)?|"
    r"do\s+not\s+forget(?:\s+that|\s+to|\s+about)?|please(?:\s+(?:remember|make\s+sure|be\s+sure)\s+to)?|"
    r"make\s+sure(?:\s+that)?(?:\s+you)?(?:\s+to)?|be\s+sure\s+to|you\s+(?:will\s+)?(?:need|have)\s+to|"
    r"you\s+must|you\s+should|you'?ll\s+need\s+to|also|and|so|ok(?:ay)?|alright|class|students|"
    r"everyone|all|guys|period\s+\d+)\b[,:]?\s+",
    r"^(?:we|you)(?:'ll|\s+will|\s+are\s+going\s+to|\s+are)?\s+(?:have|be\s+having|be\s+taking|take|"
    r"taking|be\s+doing|do|be\s+writing)\s+(?:an?|the|our|your)?\s*",
    r"^there\s+(?:will\s+be|is|are|'s)\s+(?:an?|the)?\s*",
    r"^(?:it|this|that)\s+(?:is|'s)\s+",
    r"^(?:the|an?|your|our|this|my)\s+(?=\w)",
)]
_CUE_BEFORE_RE = re.compile(
    r"(?:\b(?:which|that)\s+)?(?:\b(?:is|are|will\s+be|it'?s)\s+)?(?:\b(?:now\s+)?due\s+)?"
    r"(?:\b(?:on|by|before|until|till|no\s+later\s+than|for|at|of)\s+)?$", re.I)
_TRAILING_RE = re.compile(r"(?:[\s,;:\-–—(/]+|\b(?:and|or|but|so|on|by|at|for|is|are|due|which|"
                          r"that|the|to|of|in|will\s+be|will|be|it)\b)+$", re.I)
_GENERIC = {"test", "quiz", "exam", "homework", "hw", "assignment", "project", "essay", "lab",
            "worksheet", "midterm", "final", "reading", "presentation", "packet", "review"}


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\(\s*\)", "", s)
    for _ in range(4):
        before = s
        for rx in _LEADING:
            s = rx.sub("", s).strip()
        s = _TRAILING_RE.sub("", s).strip(" .!?,;:")
        if s == before:
            break
    if len(s) > TITLE_MAX:
        s = s[:TITLE_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return s[:1].upper() + s[1:]


_TITLE_CUTS = [re.compile(p, re.I) for p in (
    r"\s*\(\s*\d+(?:\.\d+)?\s*(?:points?|pts?)\s*\)",
    r",?\s*(?:worth|for|out\s+of)\s+\d+(?:\.\d+)?\s*(?:points?|pts?)\b",
    CLASS_TIME_RE.pattern,
    r"\s+(?:deadline|due\s+date)?\s*(?:has|have)?\s*(?:been|was|were|is|are|got|will\s+be)\s+"
    r"(?:extended|moved|pushed(?:\s+back)?|postponed|rescheduled|changed|delayed)\b.*$",
    r"\s+(?:deadline|due\s+date)$",
    r"(?<=\w)\s+(?:originally|previously|(?:that|which)\s+was)\b.*$",
)]
_CONTINUES_RE = re.compile(r"^\s*(?:on|about|over|covering|for|from|of|in|with)\b", re.I)


def _title(text: str, sent: tuple[int, int], mentions: list[Mention], due: Mention | None) -> str:
    """The sentence as a title, with its dates and their cue words cut out. A subject-first
    sentence ("Lab 5 is due Friday and …") keeps what comes before the deadline, plus a
    phrase that continues it ("Quiz on Friday on sections 2.1-2.3"); an imperative one with
    the deadline first ("By Friday, read …") keeps what comes after."""
    a, b = sent
    s = text[a:b]
    # Each date and its cue words become a marker: \x01 for the deadline, \x00 for others.
    for m in sorted((m for m in mentions if a <= m.start and m.end <= b), key=lambda m: -m.start):
        head = s[:m.start - a]
        cut = _CUE_BEFORE_RE.search(head).start()
        end = m.end - a
        if s[end:end + 1] == ")" and "(" in s[cut:end]:
            end += 1
        s = s[:cut] + (" \x01 " if m is due else " \x00 ") + s[end:]
    if "\x01" in s:
        head, tail = s.split("\x01", 1)
        tail = tail.replace("\x00", " ")
        if len(_clean(head.replace("\x00", " ")).split()) >= 2 or _WORK_RE.search(head) \
                or _ASSESSMENT_RE.search(head) or not _clean(tail):
            s = head + (" " + _first_sentence(tail) if _CONTINUES_RE.match(tail) else "")
        else:
            s = re.sub(r"^\W+", "", tail)
    s = _first_sentence(s.replace("\x00", " "))
    for t in reversed(list(TIME_RE.finditer(s))):  # a time that went with a date elsewhere
        head = s[:t.start()]
        s = head[:_CUE_BEFORE_RE.search(head).start()] + " " + s[t.end():]
    s = re.split(r"\s+[-–—]\s+(?=[A-Z])|\s+(?:and|but|so)\s+(?:must|should|will|is|are|it|this)\b", s)[0]
    for rx in _TITLE_CUTS:
        s = rx.sub("", s)
    s = re.sub(r"^(\W*\w+(?:\W+\w+)+?)\s+(?:is|are)\s+(?:now\s+)?due\b.*$", r"\1", s, flags=re.I)
    return _clean(s)


def _first_sentence(s: str) -> str:
    spans = sentences(s)
    return s[spans[0][0]:spans[0][1]] if spans else s


def _templates(titles: Iterable[str]) -> dict[str, str]:
    """How the course names numbered work: lowercased words before a number ("lab",
    "unit 3 progress check" -> "unit") -> the teacher's casing, for names used twice or more."""
    seen: Counter[str] = Counter()
    casing: dict[str, Counter[str]] = {}
    for t in titles:
        for m in re.finditer(r"\b((?:[A-Za-z][\w'-]*\s+){0,2}[A-Za-z][\w'-]*)\s*#?\s*\d+[a-z]?\b", t):
            words = m.group(1).split()
            for k in range(1, len(words) + 1):
                name = " ".join(words[-k:])
                seen[name.lower()] += 1
                casing.setdefault(name.lower(), Counter())[name] += 1
    return {k: casing[k].most_common(1)[0][0] for k, n in seen.items() if n >= 2 and len(k) > 2
            and k not in ("the", "and", "for", "due", "page", "pages", "chapter of")}


def _named(text: str, templates: dict[str, str]) -> list[tuple[int, int, str]]:
    """Spans of `text` naming work the way the course does ("lab 5" -> "Lab 5")."""
    out = []
    for name, cased in sorted(templates.items(), key=lambda kv: -len(kv[0])):
        for m in re.finditer(rf"\b{re.escape(name)}\s*#?\s*(\d+[a-z]?)\b", text, re.I):
            if all(m.end() <= a or m.start() >= b for a, b, _ in out):
                out.append((m.start(), m.end(), f"{cased} {m.group(1)}"))
    return out


# -- related items ------------------------------------------------------------------------

_STOP = set("""a an the and or but so of to in on at by for from with about into over is are
was were be been being it its this that these those you your yours we our us i me my they
them their he she his her will would can could should must may might do does did done have has
had not no yes if then than as up out all any each some more most very just also please due
today tomorrow tonight week class""".split())


def _tokens(s: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z]+|\d+", s.lower()):
        if w in _STOP or (len(w) == 1 and not w.isdigit()):
            continue
        if w == "quizzes":
            w = "quiz"
        elif len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def _numbered(s: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for w, n in re.findall(r"([a-z]+)\s*#?\s*(\d+)", s.lower()):
        out.setdefault(w.rstrip("s"), set()).add(n)
    return out


def related(text: str, ref: datetime, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Existing items the announcement may be about: the share of each title's words (by
    rarity in the course) that the announcement contains. Titles whose number differs
    ("Lab 4" in a post about lab 5) and items due far from the post count less."""
    docs = [(it, set(_tokens(it["title"] or ""))) for it in items]
    n = len(docs) or 1
    df = Counter(t for _, toks in docs for t in toks)
    idf = {t: math.log(1 + n / c) for t, c in df.items()}
    words = set(_tokens(text))
    nums = _numbered(text)
    out = []
    for it, toks in docs:
        if not toks:
            continue
        shared = toks & words
        # One shared word ("homework") says little, unless it's all of a numbered title.
        if len(shared) < 2 and not (shared == toks and any(t.isdigit() for t in toks)):
            continue
        score = sum(idf[t] for t in shared) / sum(idf[t] for t in toks)
        for w, ns in _numbered(it["title"] or "").items():
            if w in nums and not ns & nums[w]:
                score *= 0.2
        if it.get("due_at"):
            days = abs((datetime.fromisoformat(it["due_at"]) - ref).days)
            if days > 60:
                score *= 0.5
        if score >= 0.5:
            out.append({"source": it["source"], "id": it["id"], "title": it["title"],
                        "kind": it["kind"], "due_at": it.get("due_at"), "score": round(score, 2)})
    out.sort(key=lambda r: -r["score"])
    return out[:MAX_RELATED]


def usual_time(items: list[dict[str, Any]], kind: str | None = None) -> tuple[int, int] | None:
    """The time of day most of the course's deadlines are set at, if one clearly is
    (three or more, half of them at the same minute). Per kind when there are enough."""
    def mode(its: list[dict[str, Any]]) -> tuple[int, int] | None:
        times = Counter()
        for it in its:
            if due := it.get("source_due_at"):
                t = datetime.fromisoformat(due).astimezone(LOCAL_TZ)
                times[(t.hour, t.minute)] += 1
        total = sum(times.values())
        if total >= 3:
            hm, k = times.most_common(1)[0]
            if k * 2 >= total:
                return hm
        return None
    return (kind and mode([i for i in items if i["kind"] == kind])) or mode(items)


# -- drafts -------------------------------------------------------------------------------

@dataclass
class _Task:
    sent: tuple[int, int]
    score: float
    due: Mention | None
    extra: list[tuple[int, int]] = field(default_factory=list)  # sentences that follow it


def draft(announcement: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Assignment drafts for an announcement (an Item as the API returns it), using `items`,
    the rest of its course, as the student's data. See the module docstring."""
    text = announcement.get("description") or announcement.get("title") or ""
    posted = announcement.get("posted_at")
    ref = (datetime.fromisoformat(posted).astimezone(LOCAL_TZ) if posted
           else datetime.now(LOCAL_TZ))
    work = [i for i in items if i["kind"] != "announcement" and i["kind"] != "material"]
    usual = usual_time(work)
    mentions = find_dates(text, ref, usual)
    sents = clauses(text, sentences(text) or [(0, len(text))], mentions)

    def sent_of(m: Mention) -> tuple[int, int]:
        return next((s for s in sents if s[0] <= m.start < s[1]), sents[-1])

    for m in mentions:
        m.score = _deadline_score(m, text, sent_of(m), ref)
    best_overall = max((m for m in mentions if m.score > 0), key=lambda m: m.score, default=None)

    tasks: list[_Task] = []
    for s in sents:
        own = [m for m in mentions if s[0] <= m.start < s[1]]
        best = max(own, key=lambda m: m.score, default=None)
        score = _task_score(text[s[0]:s[1]], best.score if best else None)
        prev = tasks[-1] if tasks else None
        has_noun = _WORK_RE.search(text[s[0]:s[1]]) or _ASSESSMENT_RE.search(text[s[0]:s[1]])
        if score >= 2.5 and prev and prev.sent[1] <= s[0] and not has_noun and prev.extra == [] \
                and prev.due is None and best is not None and best.score > 0:
            prev.due, prev.score = best, prev.score + best.score  # "Do the lab. Submit it by Friday."
            prev.extra.append(s)
        elif score >= 2.5:
            tasks.append(_Task(s, score, best if best and best.score > 0 else None))
        elif tasks and not _SOCIAL_RE.search(text[s[0]:s[1]]):
            tasks[-1].extra.append(s)
    if not tasks:  # nothing reads like work: offer the post's first sentence anyway
        first = next((s for s in sents if not _SOCIAL_RE.search(text[s[0]:s[1]])), sents[0])
        tasks = [_Task(first, 0, best_overall)]
    tasks = sorted(tasks, key=lambda t: -t.score)[:MAX_DRAFTS]

    templates = _templates(i["title"] for i in work if i.get("title"))
    rel = related(text, ref, work)
    drafts = []
    upcoming = [m for m in mentions if not m.past and m.at.date() >= ref.date()]
    for t in tasks:
        due = t.due or best_overall or max(upcoming, key=lambda m: m.score, default=None)
        sent_text = text[t.sent[0]:t.sent[1]]
        title = _title(text, t.sent, mentions, t.due)
        if len(title) < 2:  # "It's due a week from today.": name it after what came before
            before = [x for x in sents if x[1] <= t.sent[0]]
            title = _clean(text[before[-1][0]:before[-1][1]]) if before else ""
            title = title or _clean(announcement.get("title") or "")
        options = [title]
        for a, b, name in _named(text[t.sent[0]:t.sent[1]], templates):
            if name.lower() not in title.lower():
                options.append(name)
            title = re.sub(re.escape(sent_text[a:b]), name, title, count=1, flags=re.I)
        options[0] = title
        if title.lower() in _GENERIC and (topic := _TOPIC_RE.search(sent_text) or _TOPIC_RE.search(text)):
            options.insert(0, f"{topic.group(1).title().rstrip('.')} {topic.group(2).upper() if topic.group(2).isalpha() else topic.group(2)} {title.lower()}")
        if len((first := _clean(text[sents[0][0]:sents[0][1]])).split()) >= 3:
            options.append(first)
        titles = list(dict.fromkeys(o for o in options if o and len(o) > 1))
        kind = "quiz" if _ASSESSMENT_RE.search(sent_text) else "assignment"
        if due is not None and due.time != "text":  # quizzes may have a usual time of their own
            hm = usual_time(work, kind)
            due = Mention(due.start, due.end, due.text,
                          due.at.replace(hour=(hm or DEFAULT_TIME)[0], minute=(hm or DEFAULT_TIME)[1]),
                          "course" if hm else "default", due.has_date, due.vague, due.past, due.score)
        points = _POINTS_RE.search(" ".join(text[a:b] for a, b in [t.sent, *t.extra])) or (
            len(tasks) == 1 and _POINTS_RE.search(text))
        drafts.append({
            "title": titles[0] if titles else "(untitled)",
            "titles": titles[1:],
            "kind": kind,
            "due_at": due.at.isoformat() if due else None,
            "due": due.out() if due else None,
            "description": text.strip(),
            "points_possible": float(points.group(1)) if points else None,
            "sentence": sent_text.strip(),
        })
    return {
        "drafts": drafts,
        "dates": [m.out() for m in mentions],
        "related": rel,
        "moves_deadline": bool(_MOVED_RE.search(text)),
        "usual_time": f"{usual[0]:02d}:{usual[1]:02d}" if usual else None,
    }
