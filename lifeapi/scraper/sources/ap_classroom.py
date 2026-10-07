"""College Board AP Classroom: AP Daily videos and assessments (topic questions, progress
checks, teacher-authored quizzes) for every AP subject.

The scraper loads the app's own pages and reads the JSON they fetch, so it rides on the
app's auth instead of reimplementing it:
  fym/graphql `me`                                      -> subjects + class sections
  fym/assessments/api/chameleon/student_assignments/<subject>?status=<assigned|upcoming|completed>
Nothing is opened or started.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from patchright.async_api import Page, Response
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from ... import config
from ...models import Course, Item, ItemKind, ScrapeResult
from ..auth.collegeboard import collegeboard_login, is_collegeboard_login_url, on_collegeboard_login
from ..base import Source, register
from ..browser import dump_debug, wait_for_url
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


async def _wait_event(event: asyncio.Event) -> bool:
    try:
        await asyncio.wait_for(event.wait(), config.timeout(30_000) / 1000)
        return True
    except asyncio.TimeoutError:
        return False


@register
class APClassroom(Source):
    name = "ap_classroom"

    async def scrape(self) -> ScrapeResult:
        page = await self.new_page()
        try:
            with self.step("signing in and loading the AP Classroom profile"):
                me = await self._login_and_get_me(page)
            result = ScrapeResult()
            sections = {str(s["masterSubjectId"]): s for s in me.get("sections") or []}
            for subj in me.get("studentSubjects") or []:
                course = self._course(subj, sections.get(str(subj["id"])))
                result.courses.append(course)
                with self.step(f"reading assignments for {course.name}"):
                    result.items += await self._assignments(page, course)
            return result
        finally:
            await page.close()

    async def _login_and_get_me(self, page: Page) -> dict[str, Any]:
        # The app asks GraphQL for the user profile (with `studentSubjects`) on load; the
        # operation name varies by page, so match on the payload instead.
        profiles: list[dict] = []
        got_profile = asyncio.Event()

        async def collect(r: Response) -> None:
            if "fym/graphql" in r.url and r.request.method == "POST":
                try:
                    data = (await r.json()).get("data") or {}
                except Exception:
                    return
                if data.get("studentSubjects") is not None:
                    profiles.append(data)
                    got_profile.set()

        page.on("response", collect)
        try:
            await page.goto(BASE)
            # Either the app loads (session still valid) or we bounce through the CB login.
            # With an expired session the app can cancel its first redirect to the login
            # and start another, which wait_for_url rides out.
            await wait_for_url(
                page,
                lambda u: is_collegeboard_login_url(u) or "/subjects" in u or "/assignments" in u,
                timeout=config.timeout(45_000),
            )
            if on_collegeboard_login(page):
                await collegeboard_login(page)
                await wait_for_url(page, lambda u: "apclassroom.collegeboard.org" in u, timeout=config.timeout(45_000))
            if not await _wait_event(got_profile):
                # Loaded from cache before we were listening: reload once.
                await page.reload()
                await _wait_event(got_profile)
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
            # Every assignments-API response, so a timeout can say what came back instead.
            seen: list[str] = []

            def note(r: Response) -> None:
                if "/student_assignments/" in r.url:
                    seen.append(f"HTTP {r.status} {r.url.split('/student_assignments/', 1)[1]}")

            timeout = config.timeout(30_000)
            page.on("response", note)
            try:
                async with page.expect_response(
                    lambda r: "/student_assignments/" in r.url and f"status={status}" in r.url,
                    timeout=timeout,
                ) as resp:
                    await page.goto(f"{BASE}/{course.id}/assignments?status={status}")
                data = await (await resp.value).json()
            except PlaywrightTimeoutError as e:
                await dump_debug(page, f"ap_classroom_{course.id}_{status}")
                raise TimeoutError(
                    f"AP Classroom never fetched the {status!r} assignments for {course.name} "
                    f"(subject {course.id}) within {timeout // 1000}s. The page ended at {page.url}. "
                    + (f"Assignment responses it did get: {'; '.join(seen)}" if seen
                       else "It made no student_assignments requests at all (signed out, or the page didn't finish loading?)")
                ) from e
            except Exception:
                await dump_debug(page, f"ap_classroom_{course.id}_{status}")
                raise
            finally:
                page.remove_listener("response", note)
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
