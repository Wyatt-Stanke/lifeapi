"""Infinite Campus (grades and the class schedule), signed in through Google SSO.

After the browser signs in, the portal's own JSON endpoints are called with the session:
  /campus/resources/portal/grades                    -> courses x terms x grading tasks
  /campus/resources/portal/grades/detail/<section>   -> categories + assignment scores
  /campus/api/campus/grading/gpas/my/gpa             -> overall GPAs (cumulative, maybe term)
  /campus/resources/portal/roster                    -> each section's periods, per term and
                                                        period schedule (rotation day)
  /campus/resources/calendar/instructionalDay        -> each school day's period schedule

The `gpa` partial fetch calls only the GPA one.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from urllib.parse import urlencode, urlparse

from patchright.async_api import Page

from ... import config
from ...models import GPA_PLACES, Course, Grade, GradeEntry, ScheduleDay, SchedulePeriod, ScrapeResult
from ..auth.google import LoginError, google_login, is_google_login_url, on_google_login
from ..base import SiteUnavailable, Source, register
from ..browser import dump_debug
from ..dates import LOCAL_TZ


def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _gpa_num(v: Any) -> float | None:
    # e.g. "99.150" -> 99.15, which is the same value to all GPA_PLACES places.
    n = _num(v)
    return None if n is None else round(n, GPA_PLACES)


def _clean(s: Any) -> str | None:
    return " ".join(str(s).split()) or None if s is not None else None


def _within(day: date, start: str | None, end: str | None) -> bool:
    """Whether `day` is in a range of ISO dates where either end may be open."""
    return (not start or date.fromisoformat(start[:10]) <= day) and (
        not end or day <= date.fromisoformat(end[:10]))


def _join(*names: str | None) -> str | None:
    """Distinct names, in order, for a day two enrollments describe differently."""
    return " / ".join(dict.fromkeys(n for n in names if n)) or None


@register
class InfiniteCampus(Source):
    name = "infinite_campus"
    partials = {"gpa": "Overall GPA only"}

    async def scrape(self) -> ScrapeResult:
        u = urlparse(config.INFINITE_CAMPUS_URL)
        self.base = f"{u.scheme}://{u.netloc}"
        self.app_name = u.path.rsplit("/", 1)[-1].removesuffix(".jsp")  # e.g. "jerseycity"
        page = await self.new_page()
        try:
            with self.step("signing in to Infinite Campus"):
                await self._login(page)
            result = ScrapeResult()
            if self.partial != "gpa":
                enrollments = await self._get(page, "/campus/resources/portal/grades")
                result = await self._parse(page, enrollments)
                with self.step("reading the schedule"):
                    result.schedule = await self._schedule(page)
            gpas = await self._get(page, "/campus/api/campus/grading/gpas/my/gpa")
            result.grades += [g for g in map(self._gpa, gpas) if g.gpa is not None]
            return result
        finally:
            await page.close()

    async def _login(self, page: Page) -> None:
        await page.goto(config.INFINITE_CAMPUS_URL)
        await page.wait_for_load_state("domcontentloaded")
        if "/nav-wrapper/" in page.url:
            return  # session still valid
        sso = page.locator("#samlLoginLink")
        await sso.wait_for(timeout=config.timeout(20_000))
        await sso.click()
        # SSO goes through Google, which either asks us to sign in or redirects straight back.
        try:
            await page.wait_for_url(
                lambda u: is_google_login_url(u) or "/nav-wrapper/" in u, timeout=config.timeout(30_000)
            )
        except Exception:
            pass  # reported by the portal wait below
        if on_google_login(page):
            await google_login(page)
        try:
            await page.wait_for_url("**/nav-wrapper/**", timeout=config.timeout(60_000))
        except Exception as e:
            await dump_debug(page, "infinite_campus_login")
            raise LoginError(f"Infinite Campus login didn't reach the portal (at {page.url})") from e

    async def _get(self, page: Page, path: str) -> Any:
        with self.step(f"fetching {path}"):
            r = await page.request.get(
                self.base + path, headers={"Accept": "application/json"}, timeout=config.timeout(30_000)
            )
        if r.status >= 500:
            raise SiteUnavailable(f"GET {path} -> HTTP {r.status}")
        if not r.ok:
            raise RuntimeError(f"GET {path} -> HTTP {r.status}")
        return await r.json()

    def _url(self, route: str, **params: Any) -> str:
        q = urlencode({**params, "appName": self.app_name})
        return f"{self.base}/campus/nav-wrapper/student/portal/student/{route}?{q}"

    async def _parse(self, page: Page, enrollments: list[dict]) -> ScrapeResult:
        result = ScrapeResult()
        courses: dict[str, Course] = {}
        # Every grading task we've seen, keyed by (section, term, task). The summary lists
        # them both per term and per (year-long) course; merge both views.
        tasks: dict[tuple, dict] = {}
        detail_sections: set[str] = set()

        for enr in enrollments:
            course_lists = [t.get("courses", []) for t in enr.get("terms", [])] + [enr.get("courses", [])]
            for clist in course_lists:
                for c in clist:
                    if c.get("dropped"):
                        continue
                    sid = str(c["sectionID"])
                    courses.setdefault(sid, Course(
                        source=self.name,
                        id=sid,
                        name=c["courseName"],
                        section=_clean(c.get("sectionNumber")),
                        teacher=_clean(c.get("teacherDisplay")),
                        url=self._url("classroom/grades/student-grades",
                                      showAllTerms="false", classroomSectionID=sid),
                        extra={"course_number": c.get("courseNumber"), "room": c.get("roomName"),
                               "school": _clean(c.get("schoolName"))},
                    ))
                    for t in c.get("gradingTasks", []):
                        tasks.setdefault((sid, str(t["termID"]), str(t["taskID"])), t)
                        if t.get("hasDetail") or t.get("hasAssignments"):
                            detail_sections.add(sid)

        # Per-section detail: categories and assignment scores for each task.
        details: dict[tuple, dict] = {}
        for sid in sorted(detail_sections):
            try:
                d = await self._get(page, f"/campus/resources/portal/grades/detail/{sid}?"
                                          f"showAllTerms=false&classroomSectionID={sid}")
            except Exception as e:
                self.log.warning("No grade detail for section %s: %s", sid, e)
                continue
            for det in d.get("details", []):
                t = det["task"]
                key = (sid, str(t["termID"]), str(t["taskID"]))
                details[key] = det
                tasks[key] = {**tasks.get(key, {}), **t}

        for key, t in tasks.items():
            grade = self._grade(courses[key[0]], t, details.get(key))
            # Only keep tasks that carry a grade or graded work; empty future-term
            # placeholders aren't useful.
            if grade.letter or grade.percent is not None or grade.entries:
                result.grades.append(grade)

        result.courses = list(courses.values())
        return result

    async def _schedule(self, page: Page) -> list[ScheduleDay]:
        """Every school day's classes, as the home page's "Today's Schedule" shows them for
        one day. The roster has each section's placements: a period on one period schedule in
        one term. A period schedule is a day of the rotation (named "1", shown as "Day: 1"),
        and the calendar says which one each school day follows. Fetching the roster for a
        `_date` marks that day's placements `instructionalDay` the same way, one day per call."""
        roster = await self._get(page, "/campus/resources/portal/roster"
                                       "?_expand=%7BsectionPlacements-%7Bterm%7D%7D&_crossSite=true")
        days: dict[str, ScheduleDay] = {}
        for structure in dict.fromkeys(r["structureID"] for r in roster):  # one per enrollment
            calendar = await self._get(page, f"/campus/resources/calendar/instructionalDay"
                                             f"?structureID={structure}")
            placements = [(r, p) for r in roster if r["structureID"] == structure
                          for p in r.get("sectionPlacements") or []]
            names = {p["periodScheduleID"]: _clean(p.get("periodScheduleName")) for _, p in placements}
            terms = {p["termID"]: p["term"] for _, p in placements if p.get("term")}
            for d in calendar:
                if d.get("isSchoolDay") is False:
                    continue
                on = date.fromisoformat(d["date"])
                term = next((t for t in sorted(terms.values(), key=lambda t: not t.get("isPrimary"))
                             if _within(on, t.get("startDate"), t.get("endDate"))), {})
                meets: dict[tuple, tuple[dict, dict]] = {}
                for r, p in placements:
                    t = p.get("term") or {}
                    if (p["periodScheduleID"] == d["periodScheduleID"]
                            and _within(on, p.get("startDate") or t.get("startDate"),
                                        p.get("endDate") or t.get("endDate"))
                            and _within(on, r.get("startDate"), r.get("endDate"))):  # not dropped
                        meets.setdefault((r["sectionID"], p.get("periodID")), (r, p))
                day = days.setdefault(d["date"], ScheduleDay(
                    source=self.name, id=d["date"], date=on, url=self._url("schedule"),
                    extra={k: v for k, v in {
                        "school": _clean(next((r.get("schoolName") for r, _ in placements), None)),
                        "term_id": term.get("termID"),
                        "period_schedule_id": d["periodScheduleID"],
                        "structure_id": structure,
                    }.items() if v is not None},
                ))
                day.day = _join(day.day, names.get(d["periodScheduleID"]))
                day.term = _join(day.term, _clean(term.get("termName")))
                day.periods += [self._period(on, r, p) for r, p in meets.values()]
        for day in days.values():
            day.periods.sort(key=lambda p: (p.starts_at, p.name))
        return list(days.values())

    def _period(self, on: date, section: dict, p: dict) -> SchedulePeriod:
        def at(hms: str) -> datetime:  # e.g. "08:35:00", the school's local time
            return datetime.combine(on, time.fromisoformat(hms), LOCAL_TZ)

        return SchedulePeriod(
            name=_clean(p.get("periodName")) or str(p.get("periodSequence")),
            starts_at=at(p["startTime"]),
            ends_at=at(p["endTime"]),
            course_id=str(section["sectionID"]),
            course_name=p.get("courseName") or section["courseName"],
            teacher=_clean(p.get("teacherDisplay") or section.get("teacherDisplay")),
            room=_clean(p.get("roomName") or section.get("roomName")),
        )

    def _gpa(self, g: dict) -> Grade:
        # e.g. {"calendarID": 6779, "type": "Cumulative", "termName": null, "gpa": "99.150",
        #       "unweighted": false, "rank": null, "outOf": null, "gpaName": null}
        kind = _clean(g.get("type")) or "GPA"
        term = _clean(g.get("termName"))
        return Grade(
            source=self.name,
            id=f"gpa:{g.get('calendarID')}:{kind.lower()}:{g.get('termSeq') or ''}"
               f":{'uw' if g.get('unweighted') else 'w'}",
            course_name=_clean(g.get("gpaName")) or f"{kind} GPA",
            term=term,
            task=f"{kind} GPA",
            gpa=_gpa_num(g.get("gpa")),
            url=self._url("grades"),
            extra={
                k: v for k, v in {
                    "type": kind.lower(),
                    "weighted": not g.get("unweighted"),
                    "gpa_with_bonus": _gpa_num(g.get("gpaBonus")),
                    "bonus_points": _num(g.get("bonusPoints")),
                    "rank": g.get("rank"),
                    "rank_with_bonus": g.get("rankBonus"),
                    "out_of": g.get("outOf"),
                    "calendar_id": g.get("calendarID"),
                }.items() if v is not None
            },
        )

    def _grade(self, course: Course, t: dict, det: dict | None) -> Grade:
        # A posted grade (`score`/`percent`) wins over the in-progress one.
        letter = _clean(t.get("score")) or _clean(t.get("progressScore"))
        percent = _num(t.get("percent")) if t.get("percent") is not None else _num(t.get("progressPercent"))
        categories, entries = [], []
        for cat in (det or {}).get("categories") or []:
            prog = cat.get("progress") or {}
            categories.append({
                "name": cat.get("name"),
                "weight": _num(cat.get("weight")),
                "excluded": bool(cat.get("isExcluded")),
                "letter": prog.get("progressScore"),
                "percent": _num(prog.get("progressPercent")),
                "points_earned": _num(prog.get("progressPointsEarned")),
                "points_possible": _num(prog.get("progressTotalPoints")),
            })
            for a in cat.get("assignments") or []:
                flags = [f for f in ("missing", "late", "incomplete", "cheated", "dropped", "turnedIn")
                         if a.get(f)]
                if a.get("notGraded"):
                    flags.append("not_graded")
                comments = "\n".join(filter(None, [a.get("comments"), a.get("feedback")])) or None
                entries.append(GradeEntry(
                    name=a.get("assignmentName") or "(assignment)",
                    url=self._url(f"classroom/curriculum/resource/{a['objectSectionID']}/view",
                                  classroomSectionID=course.id) if a.get("objectSectionID") else None,
                    category=cat.get("name"),
                    due_at=a.get("dueDate"),
                    score=_clean(a.get("score")),
                    points_earned=_num(a.get("scorePoints")),
                    points_possible=_num(a.get("totalPoints")),
                    percent=_num(a.get("scorePercentage")),
                    flags=[f.lower() if f != "turnedIn" else "turned_in" for f in flags],
                    comments=comments,
                ))
        sid, term_id, task_id = course.id, str(t["termID"]), str(t["taskID"])
        return Grade(
            source=self.name,
            id=f"{sid}:{term_id}:{task_id}",
            course_id=sid,
            course_name=course.name,
            teacher=course.teacher,
            term=_clean(t.get("termName")),
            task=_clean(t.get("taskName")),
            letter=letter,
            percent=percent,
            url=self._url("classroom/grades/student-grades", showAllTerms="false",
                          selectedTermID=term_id, classroomSectionID=sid),
            categories=categories,
            entries=entries,
            extra={
                k: v for k, v in {
                    "points_earned": _num(t.get("progressPointsEarned")),
                    "points_possible": _num(t.get("progressTotalPoints")),
                    "term_gpa": _num(t.get("termGPA")),
                    "weight_percent": _num(t.get("calculationWeight")),
                    "modified_at": t.get("modifiedDate"),
                    "posted": t.get("score") is not None,
                }.items() if v is not None
            },
        )
