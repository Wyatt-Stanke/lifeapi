# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

lifeapi collects a student's schoolwork from several platforms into one SQLite database
and serves it as a read-only HTTP API. The scraper and the API are separate processes on
separate schedules. They share only `lifeapi/models.py` (pydantic models) and
`lifeapi/storage.py` (SQLite access). The scraper runs every 2 hours via launchd
(`deploy/`). The API stays up. The API's only write is queueing manual sync requests
(`POST /sync`), which the scraper picks up (see "Manual sync" below).

## Commands

```sh
.venv/bin/pip install -e .                               # deps: patchright, fastapi, uvicorn, pydantic, python-dotenv
.venv/bin/python -m lifeapi.scraper --list               # registered sources
.venv/bin/python -m lifeapi.scraper --only vhl           # one source (use this to test a change)
.venv/bin/python -m lifeapi.scraper --headed -v          # visible browser, debug logging
.venv/bin/python -m lifeapi.api --port 8000              # API; OpenAPI docs at /docs
.venv/bin/python frontend/serve.py --port 8080 --api http://127.0.0.1:8000   # explorer UI
sqlite3 data/lifeapi.db "select kind, status, count(*) from items where source='vhl' group by 1,2"
```

There's no test suite or linter. To verify a change, run the affected source with
`--only`, then query `data/lifeapi.db` or hit the API. A full Google Classroom run takes
5–8 minutes (`google_classroom` re-reads detail pages selectively; see below). To force
Classroom to re-read every detail page, clear the cache marker:
`update items set data=json_remove(data,'$.extra.detail_fetched_at') where source='google_classroom'`.

Only one scraper process can use the browser profile (`data/browser-profile/`) at a time.
`runner.run()` holds `data/run.lock` (flock) for the whole run, so a second scraper run
waits for the first. Ad-hoc exploration scripts don't take that lock, and fail or hang on
Chrome's profile lock. Wait for any running scrape to finish first.

## Credentials

Credentials come from `.env` (`GOOGLE_USERNAME`, `GOOGLE_PASSWORD`,
`COLLEGEBOARD_USERNAME`, `COLLEGEBOARD_PASSWORD`) and are read only through
`config.credential()`. Don't open `.env`. Don't log or dump request bodies from login
flows: network captures during Okta or Google sign-in contain the password. Captured
College Board API responses contain access tokens, so delete scratch captures when done.

## Architecture

### Scraper (`lifeapi/scraper/`)

- `runner.py` opens one persistent patchright Chrome context (`browser.py`) and runs each
  registered source in turn. One Google sign-in is shared by Classroom, Clever (VHL) and
  Infinite Campus. Each source is isolated: an exception records a failed row in
  `scrape_runs`, and that source's existing data is left untouched.
- `base.py`: the `Source` ABC plus the `@register` registry. A new platform is one module
  in `sources/` that's imported in `sources/__init__.py`. Sources get `self.previous`
  (their items from the last run, used as a cache) and `self.map_pages()` (a pool of
  parallel tabs; exceptions are returned, not raised).
- `auth/`: `google_login` (handles account chooser, identifier, password and consent
  screens), `collegeboard_login` (Okta identifier step, then always picks the Password
  authenticator), and `clever.launch_app` (Clever dashboard tile, which may open a new tab).
- `dates.py` parses dates the way sites display them. Google Classroom omits the year only
  for dates in the current calendar year, so it uses `prefer="current_year"`. Date-only
  values become 23:59 for deadlines and 00:00 for posted dates (`posted=True`).
- Waits: no fixed pauses (`wait_for_timeout`). Wait for a condition, and put every
  deadline through `config.timeout(ms)` (scaled by `LIFEAPI_TIMEOUT_SCALE` for slow hosts).
  Never treat a timeout as "empty": wait for the page's explicit empty state instead,
  because returning no items soft-deletes them. `browser.wait_until()` races a locator
  against a URL; `browser.wait_gone()` confirms a submitted login step went away.
- `browser.dump_debug()` writes a screenshot and HTML to `data/debug/`. Sources call it
  before re-raising on unexpected pages.
- Error context: wrap phases in `with self.step("reading X"):`. An exception escaping it
  gets a "while reading X" note, which `runner.describe_error` puts in
  `scrape_runs.error` with any chained cause and the last page URL. Bare Playwright
  timeouts don't say what they waited for, so wrap waits in steps.
- Run trail (`trail.py`): each source run records every `lifeapi.*` log line (debug
  too: `__main__` sets the logger to DEBUG and filters the console instead) plus browser
  activity (navigations, XHR/fetch responses, HTTP errors, failed requests, console
  errors). Failed runs store it in `scrape_runs.log` (the last 20 per source), served at
  `GET /runs/{id}/log`. Only URLs are logged, with credential-like query params masked.

### Per-source strategy

Prefer the platform's own JSON over the DOM wherever the frontend loads it:

- **AP Classroom**: intercepts the app's own responses rather than calling the API
  directly, because auth headers are app-managed. The profile comes from whichever
  `fym/graphql` response contains `studentSubjects` (the operation name varies by page).
  Assignments come from `student_assignments/<subject>?status=assigned|upcoming|completed`.
- **Infinite Campus**: after SSO, calls `/campus/resources/portal/grades` and
  `/grades/detail/<sectionID>` with `page.request`. The session cookie doesn't persist
  across browser launches, so it signs in every run. Grades only, by request.
- **VHL**: in-page `fetch` of `study_schedule/event_calendar/YYYY-MM` (HTML fragments
  listing the due dates) and `assignments_by_due_date?due_date=` (JSON). Never open
  activity URLs: that starts the activity.
- **Google Classroom**: DOM only. The extractors are JS strings in
  `sources/google_classroom_js.py`. CSS class names are minified and change between
  releases, so match on `data-*` attributes, ARIA labels and text patterns. Pitfalls that
  have bitten before:
  - JS kept in Python strings must be raw strings (`r"""`). A `'\n'` in a normal string
    becomes a literal newline and a JS syntax error, which `wait_for_function` reports as
    a timeout.
  - Page-level wrappers carry `data-submission-id` and other data attributes, so scope
    `closest()` checks to the main region. Teacher attachments vs the student's own work
    are told apart by the nearest `[data-material-parent-id]`'s `data-filter`: `0` is the
    teacher's, `1` is the student's.
  - Classroom redirects `/a/<id>` to `/mc/` or `/sa/` in-app and leaves the old view in
    the DOM, hidden. Always select the *visible* header and status elements.
  - Detail pages are read in parallel tabs. `browser.py` passes flags that keep
    background tabs rendering; without them, `innerText` comes back unrendered (run-on
    text, empty fields).
  - Detail headers render in stages (author • date, then points, then category), and
    nothing marks points as pending, so `DETAIL_READY_JS` also waits for the header to
    stop changing. Reading early stores `points_possible=None`. The classwork list is the
    same: rows render topic by topic and categories fill in afterwards, so
    `LIST_READY_JS` waits for no spinners and stable text. Reading a partial list
    soft-deletes the rows it missed.
  - Titles in `aria-label` keep the teacher's stray spaces, while `innerText` collapses
    them. Normalise whitespace before matching one against the other.
  - Incremental refresh (`_needs_detail`): a detail page is re-read when the item is new,
    its classwork-row signature changed, it's due or posted in the last 7 days, or its
    cached detail is more than 24h old. Otherwise cached fields are merged from
    `self.previous`.

### Storage and API

- `storage.py`: tables `courses`, `items`, `grades` and `scrape_runs`. Each row stores the
  full model JSON in `data`, plus a few indexed columns used by API filters. `save_result`
  upserts and then marks rows not seen in that run `active=0` (soft delete).
  `datetime`s are stored as UTC ISO strings in the indexed columns.
- The database is in WAL mode. Read-only connections deliberately use `mode=rw` with
  `PRAGMA query_only=ON`, not `mode=ro`: readers must be able to recreate the
  `-wal`/`-shm` files after the scraper exits. They also use `check_same_thread=False`,
  because FastAPI opens the per-request connection (the `db()` dependency) and uses it on
  different threadpool threads.
- `api/app.py`: FastAPI, read-only apart from `/sync`. The optional `LIFEAPI_API_TOKEN`
  bearer auth applies to everything except `/health`. Every list endpoint hides inactive
  rows unless `include_inactive=true`. The API imports the scraper's `REGISTRY` (to
  validate sync requests and list never-run sources in `/sources`), but never opens a
  browser.

### Finishing a sign-in by hand

`deploy/reauth.py` (stdlib only, runs on the user's machine) is the one-command fix for a
login challenge on the server. It runs `docker exec <scraper> login.sh` over SSH. That
starts Xvfb, x11vnc on the container's loopback and a headed scrape. It then serves a
random `127.0.0.1` port locally, and pipes each VNC connection through `docker exec … python -c`
into the container, so nothing listens on any network. `login.sh` stops everything
when its stdin closes (`LIFEAPI_LOGIN_STOP_ON_EOF=1`), so Ctrl-C or a dropped SSH
connection never leaves a headed Chrome holding the profile lock. The container's `sh` is
dash: its `kill` rejects `--`, so process groups are killed with `kill -TERM "-$PGID"`.
`/sources` adds `login_command` (built from `LIFEAPI_REAUTH_TARGET`) when the last run
failed with `LoginError`, and the Sync status page shows it under the error.

### Manual sync

The API can't scrape itself: in containers it runs in a different service without the
credentials. So `POST /sync` inserts a `sync_requests` row (`sources` JSON list, NULL for
every enabled source) and touches `config.SYNC_TRIGGER` (`data/sync-requested`). A waiting
request that already covers the sources is returned instead of a duplicate.

- Every `runner.run()`, under the run lock: fails requests left `running` by a run that
  died, deletes the trigger, then claims each pending request whose sources it covers. If
  any stay pending, it re-touches the trigger. Deleting before reading means a request
  queued mid-run is never missed.
- `--requested` runs the union of pending requests' sources, and returns before opening
  the browser if there are none.
- Wakeups: launchd's `com.lifeapi.sync` job `WatchPaths` the trigger. The container's
  `scrape-loop.sh` polls it every `LIFEAPI_SYNC_POLL` seconds between scheduled runs.
  Python's `run.lock` is separate from `scrape.sh`'s `scrape.lock`, because the Python
  process inherits the shell's flocked fd, and taking the same file again would deadlock.

### Frontend (`frontend/`)

A temporary, deliberately unstyled explorer: plain semantic HTML, no CSS, no build step.
It's a user-facing wrapper (Today, Upcoming, Missing, Announcements, Courses, Grades,
Search, Sync status with sync buttons), not an endpoint browser. Raw API access stays at `/api/docs`.

- `serve.py` is stdlib only. It proxies `/api/*` (and `/openapi.json`, which FastAPI's
  docs page fetches from the root) to the API, so the API needs no CORS. Every other path
  serves `index.html`. It re-reads `index.html` on each request, so page edits need only a
  browser refresh. Changes to `serve.py` need a restart.
- `index.html` holds all the JS in one inline script. A tiny `h(tag, attrs, ...kids)`
  helper builds the DOM. Views are async functions that return nodes, and the hash router
  calls them as `view(...pathArgs, params)`. Routes are `#/item/<source>/<id>`,
  `#/course/<source>/<id>`, `#/grade/<source>/<id>` and so on, so pages can be
  bookmarked. Views fetch in parallel. That relies on the API's `check_same_thread=False`
  (see above).
- Display helpers live in one place: `SOURCE_NAMES` (add new sources there), `label()`
  for statuses, `relative()`/`when()` for dates, and `DONE`, which mirrors the API's
  finished statuses.
- **Link resolver**: `<site>/<any source URL>` (e.g.
  `localhost:8080/https://classroom.google.com/u/1/c/…/a/…/details`) is rewritten to
  `#/open?url=…`, which redirects to the matching page. Google Classroom URL segments are
  base64 of the numeric IDs we store, and those IDs are global across Google accounts. So
  `parseClassroom()` strips `/u/{n}` and ignores `authuser` (multi-account support), then
  decodes the course and item IDs. Every other source falls back to `bestUrlMatch()`
  against the stored `url` of every item, course and grade. Host and path must match;
  query params break ties (Infinite Campus pages differ only by `classroomSectionID` and
  `selectedTermID`). A new source works with no resolver changes as long as its records
  store `url`. A Classroom item we haven't scraped lands on its course page.

### Data model conventions

- `Item` is the shared shape for assignments, quizzes, questions, materials and
  announcements. `url` (the deep link back to the source) is the most important field.
- Anything source-specific goes in `extra`. For Classroom, `extra.submitted_work` is the
  student's own attachments, `extra.links` are links from the description,
  `extra.list_signature` and `extra.detail_fetched_at` are cache bookkeeping.
- Infinite Campus produces `Grade` records (one per course × term × grading task), each
  with `GradeEntry` assignment scores. It produces no `Item`s.
- `status` keeps each platform's own wording, in snake_case.

## Exploring a site's structure

The approach that worked: write throwaway scripts in `data/dev/` (gitignored) that
reuse `browser_context()` and the auth helpers, save HTML and screenshots with
`dump_debug`, and log XHR/fetch responses to find JSON endpoints. Don't log request
bodies on login pages (see Credentials). Delete `data/dev/` afterwards.
