"""Google Classroom: classwork (assignments, questions, materials) and stream announcements."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta

from patchright.async_api import Page

from ... import config
from ...models import Attachment, Comment, Course, Item, ItemKind, ScrapeResult
from ..auth.google import google_login, on_google_login
from ..base import Source, register
from ..browser import dump_debug
from ..dates import now, parse_display_date
from . import google_classroom_js as js

BASE = "https://classroom.google.com"

# data-stream-item-type on classwork rows -> (kind, URL path segment)
TYPE_MAP = {"1": (ItemKind.ASSIGNMENT, "a"), "4": (ItemKind.QUESTION, "sa"), "5": (ItemKind.MATERIAL, "m")}

# The header renders in stages ("author • date", then points, then category) and nothing
# marks the points as pending: an ungraded item looks the same until they arrive. So beyond
# the explicit loading signals, wait until the header has stopped changing for `quiet` ms.
DETAIL_READY_JS = r"""quiet => {
  const visible = el => el.checkVisibility ? el.checkVisibility() : !!el.offsetParent;
  const h = [...document.querySelectorAll('[data-stream-item-id]')].find(visible);
  if (!h || !h.innerText.includes('•')) return false;
  // "date [category] • points": a second "•" with nothing after it is points still loading.
  const lines = h.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  const dot2 = lines.indexOf('•', lines.indexOf('•') + 1);
  if (dot2 >= 0 && (dot2 + 1 >= lines.length || /^(Due |No due date)/.test(lines[dot2 + 1]))) return false;
  // Spinners, and the "Your work" panel's "Loading submission details" placeholder.
  if ([...document.querySelectorAll('[role="progressbar"]')].some(visible)) return false;
  const main = h.closest('[role="main"]') || document.body;
  if (/^Loading submission details/m.test(main.innerText)) return false;
  const st = [...document.querySelectorAll('span[data-submission-id]')].find(visible);
  const first = st ? st.innerText.trim().split('\n')[0] : '';
  if (st && (!first || /loading/i.test(first))) return false;
  const s = window.__lifeapiHeader;
  if (!s || s.text !== h.innerText) {
    window.__lifeapiHeader = {text: h.innerText, since: performance.now()};
    return false;
  }
  return performance.now() - s.since >= quiet;
}"""

# Resolves once the classwork list or the stream has rendered: "rows" once it shows items,
# "empty" once it shows the empty-state message. Anything else times out rather than being
# taken for an empty class, which would soft-delete the class's items.
LIST_READY_JS = r"""([rows, empty]) => {
  if (document.querySelector(rows)) return 'rows';
  // Empty classwork pages have no [role="main"].
  const main = document.querySelector('[role="main"]') || document.body;
  return new RegExp(empty).test(main.innerText) ? 'empty' : false;
}"""
EMPTY_CLASSWORK = r"No assignments yet"
EMPTY_STREAM = r"This is where you.ll see updates for this class"

# Scrolling the stream either renders more posts (usually at once: they're prefetched) or
# shows a "Loading…" spinner while it fetches them. The stream has ended once neither has
# happened for `quiet` ms. `window.__lifeapiQuiet` is reset before each scroll.
MORE_POSTS_JS = r"""([n, quiet]) => {
  if (document.querySelectorAll('[data-stream-item-id]').length > n) return 'more';
  const s = window.__lifeapiQuiet ??= {since: performance.now()};
  const loading = [...document.querySelectorAll('[role="progressbar"]')].some(e => e.checkVisibility());
  if (loading) s.since = performance.now();
  return performance.now() - s.since > quiet ? 'end' : false;
}"""

# An item's detail page is re-read if the classwork row changed, if it's "recent"
# (anything can still change: grades, comments, status), or if the cached copy is old.
RECENT = timedelta(days=7)
FULL_REFRESH = timedelta(hours=24)
MAX_STREAM_SCROLLS = 100
# Classroom shows the year on dates only when it isn't the current year.
CY = "current_year"


def b64(id_: str) -> str:
    return base64.b64encode(id_.encode()).decode()


def snake(s: str | None) -> str | None:
    return re.sub(r"\W+", "_", s.strip().lower()).strip("_") if s else None


@register
class GoogleClassroom(Source):
    name = "google_classroom"

    async def scrape(self) -> ScrapeResult:
        page = await self.new_page()
        try:
            courses = await self._courses(page)
            result = ScrapeResult(courses=courses)
            for course in courses:
                try:
                    result.items += await self._classwork(page, course)
                    result.items += await self._announcements(page, course)
                except Exception:
                    await dump_debug(page, f"google_classroom_{course.id}")
                    raise
            return result
        finally:
            await page.close()

    # -- navigation -------------------------------------------------------------------

    async def _goto(self, page: Page, url: str, ready: str) -> None:
        await page.goto(url)
        if on_google_login(page):
            await google_login(page)
            await page.goto(url)
        await page.wait_for_selector(ready, timeout=config.timeout(30_000))

    async def _list_state(self, page: Page, rows: str, empty: str) -> str:
        """Wait for a classwork list or stream to render: "rows" or "empty"."""
        state = await page.wait_for_function(
            LIST_READY_JS, arg=[rows, empty], timeout=config.timeout(30_000)
        )
        return await state.json_value()

    # -- courses ----------------------------------------------------------------------

    async def _courses(self, page: Page) -> list[Course]:
        # /h/st is the "Enrolled" filter, so classes you teach (e.g. club rosters) are skipped.
        await self._goto(page, f"{BASE}/u/0/h/st", "li[data-course-id]")
        rows = await page.evaluate(js.COURSES_JS)
        self.log.info("Found %d enrolled classes", len(rows))
        return [
            Course(
                source=self.name,
                id=r["id"],
                name=r["name"] or r["id"],
                section=r["section"],
                teacher=r["teacher"],
                url=f"{BASE}{r['href']}" if r["href"] else f"{BASE}/u/0/c/{b64(r['id'])}",
            )
            for r in rows
        ]

    # -- classwork --------------------------------------------------------------------

    async def _classwork(self, page: Page, course: Course) -> list[Item]:
        cid = b64(course.id)
        await page.goto(f"{BASE}/u/0/w/{cid}/t/all")
        rows_sel = "li[data-stream-item-id]"
        if await self._list_state(page, rows_sel, EMPTY_CLASSWORK) == "empty":
            self.log.info("%s: no classwork", course.name)
            return []
        # Topics show 10 items until "View more" is clicked.
        more = page.locator('button[aria-label="View more posts"]:visible')
        for _ in range(50):
            if not await more.count():
                break
            n = await page.locator(rows_sel).count()
            await more.first.click()
            await page.wait_for_function(
                "([sel, n]) => document.querySelectorAll(sel).length > n",
                arg=[rows_sel, n],
                timeout=config.timeout(15_000),
            )
        rows = await page.evaluate(js.CLASSWORK_JS)

        items: list[Item] = []
        to_fetch: list[Item] = []
        for row in rows:
            kind, seg = TYPE_MAP.get(row["type"], (ItemKind.ASSIGNMENT, "a"))
            label = row["label"] or ""
            if "quiz" in label.lower():
                kind = ItemKind.QUIZ
            elif "question" in label.lower():
                kind = ItemKind.QUESTION
            date_text = row["date_text"] or ""
            due_text = date_text if date_text.startswith("Due") else None
            completed = label.lower().startswith("completed")
            item = Item(
                source=self.name,
                id=row["id"],
                kind=kind,
                title=row["title"] or "(untitled)",
                url=f"{BASE}/u/0/c/{cid}/{seg}/{b64(row['id'])}/details",
                course_id=course.id,
                course_name=course.name,
                due_text=due_text,
                due_at=parse_display_date(due_text, prefer=CY) if due_text else None,
                posted_at=(
                    parse_display_date(date_text, prefer=CY, posted=True)
                    if date_text.startswith(("Posted", "Edited")) else None
                ),
                status="completed" if completed else ("assigned" if due_text else None),
                extra={
                    "topic": row["topic"],
                    "category": row["category"],
                    "list_signature": [row["title"], date_text, label, row["comment_count"]],
                },
            )
            prev = self.previous.get(item.id)
            if self._needs_detail(item, prev):
                to_fetch.append(item)
            elif prev:
                self._merge_cached(item, prev)
            items.append(item)

        self.log.info("%s: %d classwork items, %d need detail", course.name, len(items), len(to_fetch))
        results = await self.map_pages(to_fetch, self._fill_detail, concurrency=4)
        for item, res in zip(to_fetch, results):
            # A failed refresh keeps last run's details rather than dropping them.
            if isinstance(res, BaseException) and (prev := self.previous.get(item.id)):
                self._merge_cached(item, prev)
                item.extra["list_signature"] = prev.extra.get("list_signature")  # retry next run
        return items

    def _needs_detail(self, item: Item, prev: Item | None) -> bool:
        if prev is None:
            return True
        if prev.extra.get("list_signature") != item.extra.get("list_signature"):
            return True
        fetched = prev.extra.get("detail_fetched_at")
        if not fetched or now() - datetime.fromisoformat(fetched) > FULL_REFRESH:
            return True
        cutoff = now() - RECENT
        when = item.due_at or prev.posted_at
        return when is None or when >= cutoff

    @staticmethod
    def _merge_cached(item: Item, prev: Item) -> None:
        """Carry over detail-page fields from the last run."""
        for f in ("url", "description", "author", "posted_at", "status", "points_possible",
                  "score", "attachments", "comments"):
            setattr(item, f, getattr(prev, f))
        item.extra = {**prev.extra, **item.extra}

    async def _fill_detail(self, page: Page, item: Item) -> None:
        await page.goto(item.url)
        if on_google_login(page):
            await google_login(page)
            await page.goto(item.url)
        await page.wait_for_selector("[data-stream-item-id]", state="attached", timeout=config.timeout(20_000))
        # Wait until the header has rendered its "author • date" line and the submission
        # status has finished loading. Question pages redirect (/a/ -> /mc/) and render late.
        try:
            await page.wait_for_function(
                DETAIL_READY_JS, arg=config.timeout(300), timeout=config.timeout(15_000)
            )
        except Exception:
            # Some pages (multiple-choice questions) only render in the foreground tab.
            await page.bring_to_front()
            try:
                await page.wait_for_function(
                    DETAIL_READY_JS, arg=config.timeout(300), timeout=config.timeout(15_000)
                )
            except Exception as e:
                raise RuntimeError(f"detail page never finished rendering: {item.url}") from e
        d = await page.evaluate(js.DETAIL_JS)
        if not d:
            return
        item.url = d["url"]  # canonical URL (Classroom redirects /a/ -> /sa/, /mc/...)
        item.title = d["title"] or item.title
        item.author = d["author"]
        item.description = d["description"]
        posted_raw = d["posted"] or ""
        item.posted_at = parse_display_date(posted_raw.split("(")[0], prefer=CY, posted=True) or item.posted_at
        if m := re.search(r"\(Edited (.+?)\)", posted_raw):
            item.extra["edited"] = m.group(1)
        if d["due"]:
            item.due_text = d["due"] if d["due"] != "No due date" else None
            item.due_at = parse_display_date(item.due_text, prefer=CY) if item.due_text else None
        item.points_possible = d["points_possible"]
        item.score = d["score"]
        if d["status"]:
            item.status = snake(d["status"])
        if d["category"] and not item.extra.get("category"):
            item.extra["category"] = d["category"]
        item.attachments = [Attachment(**a) for a in d["attachments"]]
        if d["links"]:
            item.extra["links"] = d["links"]
        if d["submitted"]:
            item.extra["submitted_work"] = d["submitted"]
        item.comments = [
            Comment(author=c["author"], text=c["text"], posted_at=c["posted_at"], private=c["private"])
            for c in d["comments"]
        ]
        item.extra["detail_fetched_at"] = now().isoformat()

    # -- announcements ----------------------------------------------------------------

    async def _announcements(self, page: Page, course: Course) -> list[Item]:
        cid = b64(course.id)
        await page.goto(f"{BASE}/u/0/c/{cid}")
        if await self._list_state(page, "[data-stream-item-id]", EMPTY_STREAM) == "empty":
            return []
        # The stream lazy-loads older posts as you scroll. A scroll that lands while it's
        # still rendering or prefetching is ignored, so it has ended only after two quiet
        # scrolls in a row.
        quiet = 0
        for _ in range(MAX_STREAM_SCROLLS):
            n = await page.locator("[data-stream-item-id]").count()
            await page.evaluate("() => { delete window.__lifeapiQuiet; }")
            await page.mouse.wheel(0, 30_000)
            more = await page.wait_for_function(
                MORE_POSTS_JS, arg=[n, config.timeout(1_000)], timeout=config.timeout(30_000)
            )
            state = await more.json_value()
            self.log.debug("%s: stream scroll from %d posts: %s", course.name, n, state)
            quiet = quiet + 1 if state == "end" else 0
            if quiet == 2:
                break
        else:
            self.log.warning("%s: stopped after %d stream scrolls", course.name, MAX_STREAM_SCROLLS)
        posts = await page.evaluate(js.STREAM_JS)

        items: list[Item] = []
        with_comments: list[Item] = []
        for p in posts:
            body = p["body"] or ""
            item = Item(
                source=self.name,
                id=p["id"],
                kind=ItemKind.ANNOUNCEMENT,
                title=body.split("\n")[0][:120] or "(announcement)",
                url=f"{BASE}/u/0/c/{cid}/p/{b64(p['id'])}",
                course_id=course.id,
                course_name=course.name,
                description=body or None,
                author=p["author"],
                posted_at=parse_display_date(p["posted"], prefer=CY, posted=True),
                attachments=[Attachment(**a) for a in p["attachments"]],
                extra={
                    k: v for k, v in
                    {"edited": p["edited"], "comment_count": p["comment_count"], "links": p["links"]}.items()
                    if v
                },
            )
            prev = self.previous.get(item.id)
            if p["comment_count"]:
                if prev and prev.comments and prev.extra.get("comment_count") == p["comment_count"]:
                    item.comments = prev.comments
                else:
                    with_comments.append(item)
            items.append(item)

        await self.map_pages(with_comments, self._fill_post_comments)
        self.log.info("%s: %d announcements", course.name, len(items))
        return items

    async def _fill_post_comments(self, page: Page, item: Item) -> None:
        await page.goto(item.url)
        # Wait for as many comments as the stream said the post has (wide layouts render
        # each one twice, so count distinct ids).
        try:
            await page.wait_for_function(
                """n => new Set([...document.querySelectorAll('[data-comment-id]')]
                       .map(e => e.dataset.commentId)).size >= n""",
                arg=item.extra.get("comment_count") or 1,
                timeout=config.timeout(20_000),
            )
        except Exception:
            if not await page.locator("[data-comment-id]").count():
                raise
            self.log.warning("Fewer comments than expected on %s", item.url)
        d = await page.evaluate(js.DETAIL_JS)
        if d:
            item.comments = [
                Comment(author=c["author"], text=c["text"], posted_at=c["posted_at"], private=c["private"])
                for c in d["comments"]
            ]
