"""College Board AP Classroom: AP Daily videos and assessments (topic questions, progress
checks, teacher-authored quizzes) for every AP subject.

The scraper loads the app once (signing in through College Board when the app sends it
there) and takes two things from the app's own profile request, the fym/graphql response
with `studentSubjects`: the subjects and class sections, and the Authorization header the
app sent. It then calls the assignments API from the page with that header, as the app does:
  fym/assessments/api/chameleon/student_assignments/<subject>/?status=<assigned|upcoming|completed>
Loading the app's assignments pages instead (three per subject) took minutes on the server,
long enough for College Board to end the session mid-run. Nothing is opened or started.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import Page, Response
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from ... import config
from ...models import Course, Item, ItemKind, ScrapeResult
from ..auth.collegeboard import (collegeboard_login, is_collegeboard_login_url, is_myap_login_url,
                                 on_collegeboard_login)
from ..auth.google import LoginError
from ..base import SiteUnavailable, Source, register
from ..browser import dump_debug, wait_for_url
from ..dates import now
from ..trail import redact

BASE = "https://apclassroom.collegeboard.org"
# The College Board sign-in the app itself redirects to when signed out; it returns to the app.
SIGN_IN = f"https://account.collegeboard.org/login/login?DURL={BASE}/"
ASSIGNMENTS = "/fym/assessments/api/chameleon/student_assignments"
STATUSES = ("assigned", "upcoming", "completed")
# How often one load of the app may send us to the College Board sign-in. On the server it
# has looped: sign in, load the app, sent back to the sign-in.
MAX_SIGN_INS = 2
# How often one API call is retried in place after the browser cancelled it ("Failed to
# fetch") while the page stayed on the app. On the server, every request in flight in the
# tab is sometimes cancelled at once, about when the app settles on /subjects (runs 118,
# 139). The page and its token are fine; loading the app again only walks back into it.
MAX_REFETCHES = 3

# GET an API URL from the page, the way the app does. Returns [status, JSON body or null].
FETCH_JS = r"""
async ([url, headers, ms]) => {
  const res = await fetch(url, { headers, signal: AbortSignal.timeout(ms) });
  return [res.status, res.ok ? await res.json() : null];
}
"""


@dataclass
class _Profile:
    me: dict[str, Any]       # the user's `me`, plus `studentSubjects`
    api: str                 # the API's origin, e.g. https://apc-api-production.collegeboard.org
    headers: dict[str, str]  # the Authorization header the app sent with the profile request


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
                self._profile = await self._load_app(page)
            me = self._profile.me
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

    async def _load_app(self, page: Page) -> _Profile:
        """Load the app, signing in whenever it sends us to College Board, and return the
        profile it fetched once signed in."""
        # The app asks GraphQL for the user profile (with `studentSubjects`) on load; the
        # operation name varies by page, so match on the payload instead.
        profiles: list[_Profile] = []
        got_profile = asyncio.Event()

        async def collect(r: Response) -> None:
            if "fym/graphql" in r.url and r.request.method == "POST":
                try:
                    data = (await r.json()).get("data") or {}
                except Exception:
                    return
                if data.get("studentSubjects") is not None:
                    auth = (await r.request.all_headers()).get("authorization")
                    profiles.append(_Profile(
                        me={**(data.get("me") or {}), "studentSubjects": data["studentSubjects"]},
                        api=r.url.split("/fym/", 1)[0],
                        headers={"authorization": auth} if auth else {},
                    ))
                    got_profile.set()

        page.on("response", collect)
        try:
            await page.goto(BASE)
            for sign_ins in range(MAX_SIGN_INS + 1):
                # The app settles on a subject page, or sends us to the College Board sign-in
                # when its session has ended. It decides that itself, sometimes after it has
                # fetched the profile, and can cancel its first redirect and start another,
                # which wait_for_url rides out.
                try:
                    await wait_for_url(
                        page,
                        lambda u: (is_collegeboard_login_url(u) or is_myap_login_url(u)
                                   or "/subjects" in u or "/assignments" in u),
                        timeout=config.timeout(45_000),
                    )
                except PlaywrightTimeoutError as e:
                    await dump_debug(page, "ap_classroom_load")
                    raise TimeoutError(
                        f"AP Classroom reached neither a subject page nor a sign-in page within "
                        f"{config.timeout(45_000) // 1000}s; it stopped at {redact(page.url)}") from e
                if is_myap_login_url(page.url):
                    # Logging out, the app sometimes ends on MyAP's sign-in, which waits for a
                    # click and whose Student link signs in to AP Students. Start the sign-in
                    # the app usually redirects to instead, which comes back here.
                    self.log.info("AP Classroom sent us to MyAP's sign-in; signing in to AP Classroom")
                    await page.goto(SIGN_IN)
                if not on_collegeboard_login(page):
                    break
                if sign_ins == MAX_SIGN_INS:
                    await dump_debug(page, "ap_classroom_sign_in_loop")
                    raise LoginError(
                        f"AP Classroom sent us back to the College Board sign-in after {sign_ins} sign-ins")
                # A profile fetched before the redirect came with a session the app rejected.
                profiles.clear()
                got_profile.clear()
                await collegeboard_login(page)
            if not await _wait_event(got_profile):
                # Loaded from cache before we were listening: reload once.
                await page.reload()
                await _wait_event(got_profile)
        finally:
            page.remove_listener("response", collect)
        if not profiles:
            await dump_debug(page, "ap_classroom_me")
            raise RuntimeError(f"AP Classroom didn't load the user profile (at {page.url})")
        if not profiles[-1].headers:
            raise RuntimeError("AP Classroom's profile request had no Authorization header to call its API with")
        return profiles[-1]

    async def _get(self, page: Page, path: str) -> dict[str, Any]:
        """GET `path` from the AP Classroom API with the app's Authorization header. A request
        the browser cancelled while the page stayed on the app is sent again (up to
        MAX_REFETCHES times). When the token has expired, or the request fails otherwise (the
        app navigating away to the sign-in destroys the page it ran in), loads the app again
        once for a fresh token."""
        reloaded = False
        refetches = 0
        while True:
            try:
                status, body = await page.evaluate(
                    FETCH_JS, [self._profile.api + path, self._profile.headers, config.timeout(30_000)])
            except PlaywrightError as e:
                if page.is_closed():
                    raise
                first = str(e).splitlines()[0]
                if ("Failed to fetch" in first and refetches < MAX_REFETCHES
                        and page.url.startswith(BASE)):
                    refetches += 1
                    self.log.info("Fetching %s again, the browser cancelled it: %s", path, first)
                    continue
                if reloaded:
                    raise
                why = f"the request failed ({first})"
            else:
                if status == 200:
                    return body
                if status >= 500:
                    raise SiteUnavailable(f"AP Classroom's API answered HTTP {status} for {path}")
                if status != 401 or reloaded:
                    raise RuntimeError(f"AP Classroom's API answered HTTP {status} for {path}")
                why = "its token expired"
            self.log.info("Loading AP Classroom again for a fresh token: %s", why)
            self._profile = await self._load_app(page)
            reloaded = True
            refetches = 0

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
            data = await self._get(page, f"{ASSIGNMENTS}/{course.id}/?status={status}&subject={course.id}")
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
