"""Vista Higher Learning (vhlcentral.com), reached through Clever.

VHL groups work by due date: each calendar day has one or more assignment groups (e.g.
"Lección 1: CLW/HW", 2 activities). We read the course calendar (server-rendered month
fragments) to find due dates, then VHL's own JSON endpoint for each date's groups. We
never open the activities themselves, since that starts them.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from patchright.async_api import Page

from ...models import Course, Item, ItemKind, ScrapeResult
from ..auth.clever import launch_app
from ..base import Source, register
from ..browser import dump_debug
from ..dates import LOCAL_TZ, now

M3A = "https://m3a.vhlcentral.com"
HOME = "https://www.vhlcentral.com/home"
MONTHS_BACK, MONTHS_AHEAD = 1, 2

# Each "My Dashboard" link on vhlcentral.com/home is an enrolled section; the card around it
# has the program/course/teacher text.
SECTIONS_JS = r"""
() => [...document.querySelectorAll('a[href*="m3a.vhlcentral.com/courses/"]')]
  .filter(a => /My Dashboard/i.test(a.innerText))
  .map(a => {
    let card = a;
    // Climb to the card: stop once it has a heading, or before it would hold a second section.
    while (card.parentElement && !card.querySelector('h1, h2, h3, h4') &&
           card.parentElement.querySelectorAll('a[href*="m3a.vhlcentral.com/courses/"]').length === 1) card = card.parentElement;
    const lines = card.innerText.split('\n').map(s => s.trim()).filter(Boolean)
      .filter(l => !/^(My Dashboard|Access Content|Complete Enrollment)$/i.test(l));
    return { href: a.href, lines };
  })
"""

# Fetch a month fragment of the calendar and return [date, [labels]] for days with work.
CALENDAR_JS = r"""
async (url) => {
  const res = await fetch(url, { credentials: 'include' });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  return [...doc.querySelectorAll('td[id]')]
    .filter(td => /^\d{8}$/.test(td.id))
    .map(td => [td.id, [...td.querySelectorAll('[role="link"][aria-label]')].map(e => e.getAttribute('aria-label')),
                (td.querySelector('[data-day-total-time] [aria-hidden]') || {}).textContent])
    .filter(([, labels]) => labels.length);
}
"""

JSON_JS = r"""
async (url) => {
  const res = await fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res.json();
}
"""


def _months(today: date) -> list[str]:
    out = []
    for delta in range(-MONTHS_BACK, MONTHS_AHEAD + 1):
        y, m = divmod(today.month - 1 + delta, 12)
        out.append(f"{today.year + y:04d}-{m + 1:02d}")
    return out


@register
class VistaHigherLearning(Source):
    name = "vhl"

    async def scrape(self) -> ScrapeResult:
        page = await self.new_page()
        app: Page | None = None
        try:
            app = await launch_app(self.context, page, "Vista Higher Learning")
            await app.goto(HOME)
            await app.wait_for_selector('a[href*="m3a.vhlcentral.com/courses/"]', timeout=30_000)
            sections = await app.evaluate(SECTIONS_JS)
            if not sections:
                await dump_debug(app, "vhl_no_sections")
            result = ScrapeResult()
            for s in sections:
                course, items = await self._section(app, s)
                result.courses.append(course)
                result.items += items
            return result
        finally:
            if app and app is not page:
                await app.close()
            await page.close()

    async def _section(self, app: Page, s: dict) -> tuple[Course, list[Item]]:
        m = re.search(r"/courses/([^/]+)/sections/([^/?]+)", s["href"])
        assert m, s["href"]
        # The home link uses GUIDs; the dashboard redirects to numeric ids, which the
        # calendar/JSON endpoints need.
        await app.goto(s["href"])
        await app.wait_for_load_state("domcontentloaded")
        m2 = re.search(r"/courses/(\d+)/sections/(\d+)", app.url)
        if not m2:
            await dump_debug(app, "vhl_dashboard")
            raise RuntimeError(f"Unexpected VHL dashboard URL {app.url}")
        course_num, section_num = m2.groups()
        base = f"{M3A}/courses/{course_num}/sections/{section_num}"

        lines = s["lines"]
        # Card lines look like: program title, edition, subtitle, class name, period, teacher.
        program = lines[0] if lines else None
        class_lines = [l for l in lines if re.search(r"\(\d{2}-\d{2}\)|Period|Block", l)]
        teacher = next((l for l in reversed(lines) if l.isupper() and len(l.split()) >= 2), None)
        course = Course(
            source=self.name,
            id=section_num,
            name=" | ".join(class_lines) or program or section_num,
            section=class_lines[1] if len(class_lines) > 1 else None,
            teacher=teacher,
            url=base,
            extra={"program": program, "course_id": course_num},
        )

        # Due dates: calendar months around today, plus anything still late.
        days: dict[str, dict] = {}
        for month in _months(now().date()):
            for day_id, labels, total in await app.evaluate(
                CALENDAR_JS, f"{base}/study_schedule/event_calendar/{month}"
            ):
                d = f"{day_id[:4]}-{day_id[4:6]}-{day_id[6:]}"
                days[d] = {"labels": labels, "estimated_time": (total or "").strip() or None}
        for summary in await app.evaluate(JSON_JS, f"{base}/past_assignment_summaries"):
            days.setdefault(summary["due_date"], {}).update(
                late=summary.get("incomplete"), estimated_time=summary.get("estimated_time")
            )

        items: list[Item] = []
        for d in sorted(days):
            data = await app.evaluate(JSON_JS, f"{base}/assignments_by_due_date?due_date={d}")
            for g in data.get("groups", []):
                items.append(self._item(course, d, g, days[d], data.get("due_date_start_url")))
        self.log.info("%s: %d assignment groups over %d due dates", course.name, len(items), len(days))
        return course, items

    def _item(self, course: Course, day: str, g: dict, cal: dict, day_url: str | None) -> Item:
        start = g.get("start_url") or ""
        concept = re.search(r"/concept/(\d+)", start)
        key = concept.group(1) if concept else re.sub(r"\W+", "-", g.get("title", "")).lower()
        assigned, completed = g.get("assigned_count") or 0, g.get("completed_count") or 0
        due = datetime.fromisoformat(day).replace(hour=23, minute=59, tzinfo=LOCAL_TZ)
        if assigned and completed >= assigned:
            status = "completed"
        elif due < now():
            status = "missing" if not completed else "partial_late"
        else:
            status = "partial" if completed else "assigned"
        return Item(
            source=self.name,
            id=f"{course.id}:{day}:{key}",
            kind=ItemKind.ASSIGNMENT,
            title=g.get("title") or "VHL assignment",
            url=f"{M3A}{start}" if start else (f"{M3A}{day_url}" if day_url else course.url),
            course_id=course.id,
            course_name=course.name,
            due_at=due,
            due_text=day,
            status=status,
            extra={
                "activities_assigned": assigned,
                "activities_completed": completed,
                "vhl_status": g.get("status"),
                "estimated_time": cal.get("estimated_time"),
                "availability_message": g.get("availability_message"),
                "can_be_started": g.get("can_be_started"),
                "proctored": g.get("proctoring_enabled"),
                "calendar_labels": cal.get("labels"),
                "calendar_url": f"{course.url}/study_schedule",
            },
        )
