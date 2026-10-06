"""College Board AP Classroom: AP Daily videos and assessments (topic questions, progress
checks, teacher-authored quizzes) for every AP subject.

The scraper loads the app's own pages and reads the JSON they fetch, so it rides on the
app's auth instead of reimplementing it:
  fym/graphql `me`                                      -> subjects + class sections
  fym/assessments/api/chameleon/student_assignments/<subject>?status=<assigned|upcoming|completed>
Nothing is opened or started.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from patchright.async_api import Page, Response

from ... import config
from ...models import Course, Item, ItemKind, ScrapeResult
from ..auth.collegeboard import collegeboard_login, on_collegeboard_login
from ..base import Source, register
from ..browser import dump_debug
from ..dates import now

BASE = "https://apclassroom.collegeboard.org"
STATUSES = ("assigned", "upcoming", "completed")


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


@register
class APClassroom(Source):
    name = "ap_classroom"

    async def scrape(self) -> ScrapeResult:
        page = await self.new_page()
        try:
            me = await self._login_and_get_me(page)
            result = ScrapeResult()
            sections = {str(s["masterSubjectId"]): s for s in me.get("sections") or []}
            for subj in me.get("studentSubjects") or []:
                course = self._course(subj, sections.get(str(subj["id"])))
                result.courses.append(course)
                result.items += await self._assignments(page, course)
            return result
        finally:
            await page.close()

    async def _login_and_get_me(self, page: Page) -> dict[str, Any]:
        # The app asks GraphQL for the user profile (with `studentSubjects`) on load; the
        # operation name varies by page, so match on the payload instead.
        profiles: list[dict] = []

        async def collect(r: Response) -> None:
            if "fym/graphql" in r.url and r.request.method == "POST":
                try:
                    data = (await r.json()).get("data") or {}
                except Exception:
                    return
                if data.get("studentSubjects") is not None:
                    profiles.append(data)

        page.on("response", collect)
        try:
            await page.goto(BASE)
            # Either the app loads (session still valid) or we bounce through the CB login.
            await page.wait_for_url(
                lambda u: "idp.collegeboard.org" in u or "/subjects" in u or "/assignments" in u,
                timeout=config.timeout(45_000),
            )
            if on_collegeboard_login(page):
                await collegeboard_login(page)
                await page.wait_for_url(lambda u: "apclassroom.collegeboard.org" in u, timeout=config.timeout(45_000))
            for _ in range(30):
                if profiles:
                    break
                await page.wait_for_timeout(1000)
            if not profiles:  # loaded from cache before we were listening: reload once
                await page.reload()
                for _ in range(30):
                    if profiles:
                        break
                    await page.wait_for_timeout(1000)
        finally:
            page.remove_listener("response", collect)
        if not profiles:
            await dump_debug(page, "ap_classroom_me")
            raise RuntimeError(f"AP Classroom didn't load the user profile (at {page.url})")
        data = profiles[-1]
        return {**(data.get("me") or {}), "studentSubjects": data["studentSubjects"]}

    def _course(self, subj: dict, section: dict | None) -> Course:
        sid = str(subj["id"])
        return Course(
            source=self.name,
            id=sid,
            name=subj["name"],
            section=section.get("name") if section else None,
            url=f"{BASE}/{sid}/assignments/dashboard",
            extra={"section_id": section.get("id")} if section else {},
        )

    async def _assignments(self, page: Page, course: Course) -> list[Item]:
        items: dict[str, Item] = {}
        for status in STATUSES:
            try:
                async with page.expect_response(
                    lambda r: "/student_assignments/" in r.url and f"status={status}" in r.url,
                    timeout=config.timeout(30_000),
                ) as resp:
                    await page.goto(f"{BASE}/{course.id}/assignments?status={status}")
                data = await (await resp.value).json()
            except Exception:
                await dump_debug(page, f"ap_classroom_{course.id}_{status}")
                raise
            for a in data.get("assignments") or []:
                item = self._item(course, a)
                items.setdefault(item.id, item)
        self.log.info("%s: %d assignments", course.name, len(items))
        return list(items.values())

    def _item(self, course: Course, a: dict) -> Item:
        atype = a.get("type") or "assessment"
        is_video = atype == "video"
        aid = str(a["id"])
        due = _dt(a.get("due_at"))
        filt = a.get("filter_status")
        score = a.get("score")

        if filt == "completed":
            status = "graded" if score is not None else "completed"
        elif filt == "upcoming":
            status = "upcoming"
        elif due and due < now():
            status = "missing"
        else:
            status = "assigned"

        if is_video:
            url = f"{BASE}/d/{a['url']}" if a.get("url") else f"{BASE}/{course.id}/assignments?status={filt}"
        elif filt == "completed" and a.get("display_results", True):
            url = f"{BASE}/{course.id}/assessments/results/{aid}"
        else:
            url = f"{BASE}/{course.id}/assignments?status={filt or 'assigned'}"

        extra = {
            "type": atype,
            "ap_status": a.get("status"),
            "starts_at": a.get("starts_at"),
            "submitted_at": a.get("submitted_at"),
            "unit": a.get("unit_id"),
            "labels": a.get("labels") or None,
        }
        if is_video:
            extra |= {"watched_percentage": a.get("watched_percentage"), "video_id": a.get("video_id"),
                      "resource_id": a.get("resource_id")}
        else:
            extra |= {
                "progress": {k: v for k, v in (a.get("progress") or {}).items() if v} or None,
                "timer_minutes": a.get("timer"),
                "hard_timer": a.get("is_hard_timer"),
                "secure": a.get("is_secure"),
                "allow_late_submission": a.get("allow_late_submission"),
                "awaiting_scoring": a.get("awaiting_scoring"),
                "scored_at": a.get("scoring_completed_at"),
                "actions": a.get("actions"),
            }
        return Item(
            source=self.name,
            id=f"{'video' if is_video else 'assessment'}:{aid}",
            kind=ItemKind.ASSIGNMENT if is_video else ItemKind.QUIZ,
            title=a.get("title") or "AP Classroom assignment",
            url=url,
            course_id=course.id,
            course_name=course.name,
            posted_at=_dt(a.get("starts_at")) or _dt(a.get("created_at")),
            due_at=due,
            status=status,
            score=float(score) if score is not None else None,
            points_possible=float(a["max_score"]) if a.get("max_score") is not None else None,
            extra={k: v for k, v in extra.items() if v is not None},
        )
