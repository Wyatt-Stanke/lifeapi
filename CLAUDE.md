# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

lifeapi collects a student's schoolwork from several platforms into one SQLite database
and serves it as a read-only HTTP API. The scraper and the API are separate processes on
separate schedules. They share only `lifeapi/models.py` (pydantic models) and
`lifeapi/storage.py` (SQLite access). launchd (`deploy/`) or the container loop runs the
scraper with `--due` every minute, and it fetches the sources that are due on their
schedules (see "Schedules" below). The API stays up. Its only writes are queueing manual
sync requests (`POST /sync`) and setting schedules and browser settings, all of which the
scraper picks up, and the student's own additions (notes, changed deadlines and statuses,
assignments made from announcements, commands; see "The student's additions"), which the scraper never
touches.

## Commands

```sh
.venv/bin/pip install -e .                               # deps: patchright, fastapi, uvicorn, pydantic, python-dotenv
.venv/bin/python -m lifeapi.scraper --list               # registered sources
.venv/bin/python -m lifeapi.scraper --only vhl           # one source (use this to test a change)
.venv/bin/python -m lifeapi.scraper --only infinite_campus --partial gpa   # one partial fetch
.venv/bin/python -m lifeapi.scraper --due -v             # what the schedules say is due
.venv/bin/python -m lifeapi.scraper --headed -v          # visible browser, debug logging
.venv/bin/python -m lifeapi.api --port 8000              # API; OpenAPI docs at /docs
.venv/bin/python frontend/serve.py --port 8080 --api http://127.0.0.1:8000   # explorer UI
sqlite3 data/lifeapi.db "select kind, status, count(*) from items where source='vhl' group by 1,2"
```

There's no test suite or linter. To verify a change, run the affected source with
`--only`, then query `data/lifeapi.db` or hit the API. A Google Classroom run takes about
2 minutes with a warm cache, and up to about 7 when it re-reads everything
(`google_classroom` re-reads detail pages and streams selectively; see below). To force
Classroom to re-read every detail page, clear the cache marker:
`update items set data=json_remove(data,'$.extra.detail_fetched_at') where source='google_classroom'`.
For every stream in full, clear `$.extra.read_at` the same way.

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
  registered source in turn: the headless sources first, then the headed ones in a second
  launch (see "Headed sources and Cloudflare"). One Google sign-in is shared by Classroom,
  Clever (VHL) and Infinite Campus, across both launches, since they use the same profile. Each source is isolated: an exception records a failed row in
  `scrape_runs`, and that source's existing data is left untouched.
- `base.py`: the `Source` ABC plus the `@register` registry. A new platform is one module
  in `sources/` that's imported in `sources/__init__.py`. Sources get `self.previous`
  (their items from the last run, used as a cache), `self.partial` (see "Schedules") and
  `self.map_pages()` (a pool of parallel tabs; exceptions are returned, not raised).
- `auth/`: `google_login` (handles account chooser, identifier, password and consent
  screens), `collegeboard_login` (Okta identifier step, then always picks the Password
  authenticator; it returns as soon as the flow leaves the sign-in pages, since the last
  one, `account.collegeboard.org/login/exchangeToken`, has no form), and
  `clever.launch_app` (Clever dashboard tile, which may open a new tab; a 5xx from Clever's
  portal fails at once as "Clever is down").
- `dates.py` parses dates the way sites display them. Google Classroom omits the year only
  for dates in the current calendar year, so it uses `prefer="current_year"`. Date-only
  values become 23:59 for deadlines and 00:00 for posted dates (`posted=True`).
- Waits: no fixed pauses (`wait_for_timeout`). Wait for a condition, and put every
  deadline through `config.timeout(ms)` (scaled by `LIFEAPI_TIMEOUT_SCALE` for slow hosts).
  Never treat a timeout as "empty": wait for the page's explicit empty state instead,
  because returning no items soft-deletes them. `browser.wait_until()` races a locator
  against a URL; `browser.wait_gone()` confirms a submitted login step went away.
  `browser.wait_for_url()` is `page.wait_for_url` for redirect chains: Playwright's
  rejects on any aborted navigation (`net::ERR_ABORTED; maybe frame was detached?`), even
  one the site cancels itself before redirecting elsewhere, as College Board's does.
- `browser_context()` routes every request through a no-op handler (`_continue`). Don't
  remove it. patchright intercepts every request and, without a route, continues each
  one immediately. Chrome 155 then kills its network service ("DevTools: Duplicate request
  ID", a bad-message report), which cancels every in-flight request in every tab
  (`ERR_ABORTED`, "Failed to fetch", Classroom's "Failed to retrieve dependencies of
  service") every 10 to 30 s, and can leave a page load hung. To count restarts, watch the
  PID of the process with `network.mojom.NetworkService` in its command line. Kills also
  leave dumps in `~/.config/google-chrome/Crash Reports/completed`, but throttled, so not
  one per kill. The dumps hold the environment, credentials included: never read them.
  Chrome 154 and vanilla Playwright are unaffected. The route also turns off the HTTP cache.
- `browser.dump_debug()` writes a screenshot and HTML to `data/debug/`. Sources call it
  before re-raising on unexpected pages. The name is fixed per place (no run id), so the next
  failure there replaces it. The API serves them (`GET /debug`, `GET /debug/{file}`), and
  `GET /runs/{id}` lists a run's by finding the "Saved debug snapshot" lines in its trail.
- Time limit: `runner._scrape` stops a source still running after `LIFEAPI_SOURCE_TIMEOUT`
  seconds (default 1800) and records a `SourceTimeout`, with its trail and the steps in
  progress (`Source.active_steps`). Before it existed, a Classroom run stuck in a call with no
  deadline of its own (`page.evaluate`) held every source for 7 hours and left no trail. A
  stopped scrape gets `CANCEL_GRACE_SECONDS` to close its tabs, then the run moves on
  without it, and `browser_context()` bounds its own `ctx.close()` for the same reason.
- Error context: wrap phases in `with self.step("reading X"):`. An exception escaping it
  gets a "while reading X" note, which `runner.describe_error` puts in
  `scrape_runs.error` with any chained cause and the last page URL. Bare Playwright
  timeouts don't say what they waited for, so wrap waits in steps.
- Whose fault: `runner.failure_kind` files each failed run under `scrape_runs.failure`
  (the API's `failure`): `login` for a `LoginError` anywhere in the chain, `site` for
  `base.SiteUnavailable` or one of Chrome's can't-connect errors, `scraper` for anything
  else. When a site answers an HTTP 5xx, raise `SiteUnavailable`, so an outage (Clever's
  portal answering 503, say) doesn't read as a scraper bug or offer a sign-in fix.
- Run trail (`trail.py`): each source run records every `lifeapi.*` log line (debug
  too: `__main__` sets the logger to DEBUG and filters the console instead) plus browser
  activity (navigations, XHR/fetch responses, HTTP errors, failed requests, console
  errors). Failed runs store it in `scrape_runs.log` (the last 20 per source), served at
  `GET /runs/{id}/log`. Only URLs are logged, with credential-like query params masked.

### Per-source strategy

Prefer the platform's own JSON over the DOM wherever the frontend loads it:

- **AP Classroom**: loads the app once and takes the profile from whichever `fym/graphql`
  response contains `studentSubjects` (the operation name varies by page), along with the
  `Authorization` header the app sent with it. With that header it fetches
  `student_assignments/<subject>/?status=assigned|upcoming|completed` from the page (all
  subjects in about 3 s). Loading the app's assignments pages instead, three per subject,
  took minutes on the server, and College Board ended the session mid-run. The app checks
  its session's expiry itself and can send a reused session to the College Board sign-in
  even after it has fetched the profile, so `_load_app` waits for it to settle on
  `/subjects` (signing in again, up to `MAX_SIGN_INS`) and keeps only the profile fetched
  after the last sign-in. Logging out, the app sometimes ends on `myap.collegeboard.org/login`
  instead: a Student/Educator chooser that never redirects, whose Student link signs in to
  AP Students. `_load_app` goes from there to `SIGN_IN`, the sign-in the app normally
  redirects to. A 401 (expired token: the API answers 422 for a malformed one)
  loads the app again once for a fresh token. A "Failed to fetch" while the page is still
  on the app is retried in place first (`MAX_REFETCHES`). On the server, everything in flight
  in the tab sometimes gets cancelled at once, about when the app settles on `/subjects`
  (runs 118, 139). Loading the app again walks straight back into it.
  On the server, College Board's sign-in (behind Akamai) fails headless from the datacenter IP:
  the `mslogin…/auth/url` or `/auth/exchange` request is cancelled, and the page lands on
  `account.collegeboard.org/login/error`. It passes headed (run 139), so set the source to
  headed there.
- **Infinite Campus**: after SSO, calls `/campus/resources/portal/grades` and
  `/grades/detail/<sectionID>` with `page.request`, plus `/campus/api/campus/grading/gpas/my/gpa`
  for overall GPAs (this district shows only a weighted cumulative GPA, as a percentage that weighting can push past 100). The session cookie doesn't persist
  across browser launches, so it signs in every run. Grades only, by request. Its `gpa`
  partial fetch signs in and calls only the GPA endpoint.
- **VHL**: in-page `fetch` of `study_schedule/event_calendar/YYYY-MM` (HTML fragments
  listing the due dates) and `assignments_by_due_date?due_date=` (JSON). Never open
  activity URLs: that starts the activity. vhlcentral.com is behind Cloudflare, which
  challenges the server's datacenter IP, so VHL runs headed by default and calls
  `pass_challenge` after every navigation. The first call matters most: Cloudflare can
  challenge Clever's sign-in callback, and navigating away before it clears throws away
  the one-time `?code=`.
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
  - Detail pages are read in parallel tabs (`_detail_worker`), while the main tab goes on
    to the next class's list. Streams are read after every detail page is done.
    `browser.py` passes flags that keep background tabs rendering; without them,
    `innerText` comes back unrendered (run-on text, empty fields).
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
    cached detail is more than 24h old (7 days for items due or posted over 90 days ago).
    Otherwise cached fields are merged from `self.previous`.
  - Incremental streams (`_announcements`): the stream is scrolled only until the last
    rendered announcement is a cached one (posts are newest first), and older cached posts
    are carried over unchanged. A class's stream is read to the end, so edits, comments
    and deletions on old posts show up, once its oldest post's `extra.read_at` is over
    24h old. Classes with no cached announcements are always read to the end.
  - Both age-based re-reads are paced by `_due`: besides everything past its limit, each
    run re-reads the oldest share that the time since the last successful full run
    (`Source.last_run_at`) is of the limit. At a 2h schedule that's 1/12 of them a run;
    at a daily one, all of them. Without it they all come due in the same run.

### Storage and API

- `storage.py`: tables `courses`, `items`, `grades` and `scrape_runs`, plus `sync_requests`,
  `schedules`, `browser_settings`, `history`, and the API-written `item_marks` and
  `custom_items` and `actions` (see "The student's additions"). Each record row stores the full model JSON in `data`, plus a few indexed
  columns used by API filters. `save_result` upserts and then, for a full run, marks rows
  not seen in that run `active=0` (soft delete). `datetime`s are stored as UTC ISO strings
  in the indexed columns. New columns go in both `SCHEMA` and `_migrate`.
- Retention, for a server that runs for years: records are never deleted (only marked
  inactive), but they hold only their latest values. `save_result` appends every change to
  a grade-related value to `history` (grade letter/percent/GPA/categories, each `entries`
  score, item status/score; the fields are in `_history_values`), comparing against the
  latest row per record, so it's a few KB a week and is never pruned. Add a field there to
  track it. `storage.prune()`, called by each run that scrapes something, deletes
  `scrape_runs` and finished `sync_requests` older than `KEEP_RUNS_DAYS` (180), except each
  source's latest run, latest full run and latest successful full run, which `next_fetch`
  and `/sources` need. Failed-run logs are capped separately (`KEEP_RUN_LOGS`). Compose
  rotates container logs (`x-logging`). The API's `db()`
  runs `init_db()` once per process, so reads work on a database an older scraper made.
- The database is in WAL mode. Read-only connections deliberately use `mode=rw` with
  `PRAGMA query_only=ON`, not `mode=ro`: readers must be able to recreate the
  `-wal`/`-shm` files after the scraper exits. They also use `check_same_thread=False`,
  because FastAPI opens the per-request connection (the `db()` dependency) and uses it on
  different threadpool threads.
- `api/app.py`: FastAPI, read-only apart from `/sync`, `/sources/{source}/schedule`,
  `/sources/{source}/browser`, `/items/{source}/{item_id}/note` and `/due`,
  `/extra/assignments` and `/extra/commands`. The
  optional `LIFEAPI_API_TOKEN` auth (bearer header or `?token=` query param) applies to
  everything except `/health` and the single-value endpoints `/min/<name>` (plain-text number)
  and `/json/<name>` (`{"value": …}`), for `gpa`, `missing`, `next`, `due` and `status`.
  Those are public so the `/big/<name>` pages and widgets work on any device; `_value()` registers both
  forms from one function, copying its signature so query params (`?days=`) work on both. Every list endpoint hides inactive rows unless
  `include_inactive=true`. The API imports the scraper's `REGISTRY` (to validate sync
  requests and schedules, and list never-run sources and their partials in `/sources`), but
  never opens a browser.
  `GET /db` returns the whole database (SQLite backup API into a temp file, deleted after
  sending), for copying the server's data to a local install. `GET /debug/{file}` serves a
  snapshot's HTML as `text/plain`: it's a third-party page, and served as HTML its scripts
  would run on the API's origin (where the explorer keeps the token), and `serve.py` drops
  any other header that could sandbox it.
- The OpenAPI spec (`/openapi.json`) is meant to be handed to a person or agent on its own,
  so it's the API's documentation. The overview (common questions, sources, ids, statuses,
  time zones) is `DESCRIPTION` in `api/schemas.py`. Field docs are the `Field(description=)`s
  in `models.py`. Response models are in `api/schemas.py`; they're validated on output, so a
  stored row that no longer fits its model becomes a 500. When you add a source, status,
  `extra` key or endpoint, update those docs. `DONE_STATUSES` (in `models.py`, re-exported
  there) is the finished-status list that `/items/upcoming` and `item_view` use.

### The student's additions

The API writes these, in tables the scraper never reads or writes, so a scrape (including
`save_result`'s soft delete) can't overwrite them:

- `item_marks` (`source`, `id`, `note`, `due_at` UTC, `status`): a note and/or the student's
  own deadline on any item, scraped or custom, and on scraped items their own status
  (`turned_in` or `assigned`; custom items keep theirs in `custom_items`). `storage.set_mark`
  deletes a row once all three are null.
- `custom_items`: assignments made from an announcement (`POST /extra/assignments`), in the
  announcement's source and course (`from_id`), served with id `lifeapi-<custom_id>`
  (`storage.CUSTOM_PREFIX`). `data` is the Item JSON with `due_at` null: their deadline is an
  `item_marks` row like any changed deadline, so one code path handles both. `status` is
  `assigned` or `done`. Deleting one deletes its marks.
- `item_view` (a SQL view) is what the API serves items from: `items` UNION ALL
  `custom_items`, LEFT JOIN `item_marks`. Its `due_at` is the effective deadline
  (`COALESCE(mark, source's)`), so every filter, sort and count (`_due_sql`, `_missing_sql`,
  `/items`) uses the student's deadline without special cases, and `source_due_at` is the
  source's. Its `status` is the mark's while the mark and the source's disagree on whether
  the item is finished (`DONE_STATUSES`), else the source's, so a `turned_in` mark doesn't
  hide a later `graded`; `source_status` is the source's. API item queries must use
  `item_view`, never `items`, and `storage.item_to_dict` to build the response (it adds
  `source_due_at`, `source_status`, `converted_from` and `user`). `_migrate`
  recreates the view whenever `ITEM_VIEW`'s text changes, so edit it there. The scraper's
  `_previous_items` reads `items` directly, so its cache never sees marks.

- `actions`: every command that changed something (`POST /extra/commands`), and every change made
  through `PUT`/`DELETE /items/{source}/{item_id}/note`, `/due` and `/status` (`app._edit`, with
  `command` like `set the due date`; the plans are `commands.due_plan`, `note_plan` and so on), with `before`
  and `after` JSON of the fields it touched (`due_at` and `status` as item_marks stores them,
  so null means the source's; `note`; a custom item's `status`). Undoing applies `before` and is logged as an action with
  `undo_of`; `storage.record_action` keeps `undone_by` meaning "not in effect" down a
  redo chain (undoing an undo clears its target's `undone_by`). Any action can be undone
  however old; if its fields changed since, the later change is overwritten and the undo's
  `notes` say so. `kind` (`command`, `undo`, `redo`) is computed in the API's `ACTIONS_SQL`.

`api/when.py` is the temporal tagger both of the next two use: regexes for date parts and
time parts, resolved against a reference time, joined when adjacent (`_JOIN_RE`), as `Spec`s
that keep what wasn't said as None. `find_dates` resolves them for posts (against
`posted_at`, a missing time from the course's habit). `parse(command=True)`, after
`normalize()`, is the forgiving mode for commands: lowercase "wed"/"may" are dates, "10-9"
is a date, bare "at 5" and "11:59" count, with `ampm=False` so the caller picks the half of
the day; `normalize` fixes misspelt weekdays and months (difflib, cutoff 0.8) and spoken
numbers ("eleven fifty nine pm"). It's rules on purpose: on posts and commands it beat
dateparser (13/32), parsedatetime (18/32), ctparse (18/32) and Microsoft Recognizers-Text
(25/32, and its Python port no longer installs cleanly) against 31/32, because they read
ordinary words and numbers as dates ("sat", "may", "now", "in a second", "1-5", "2.1").
For titles, a spaCy dependency parse scored 9/20 against the rules' 20/20 on teacher
shorthand ("HW: p. 45 #1-10 due tomorrow"), for ~250 MB of dependencies. Don't swap
either in without re-running such a comparison.

`api/drafts.py` makes `GET /extra/drafts/{source}/{item_id}`: suggestions for the
"convert to assignment" form, from the announcement's text and the rest of its course. It's
deliberately rule-based (no model, no dependency, ~2 ms a post), so it's deterministic and
each suggestion carries its evidence (the date's text and offsets, where its time came from,
the sentence). The pipeline: `when.find_dates` -> `clauses` (sentences, split between dated clauses) -> `_deadline_score` and
`_task_score` (cue words around each date and in each sentence) -> `_title` (cuts the
deadline and its cue words, keeps the subject or the imperative, trims reminder phrasing).
Course data fills the gaps: `usual_time` (the modal due time of the course's scraped
deadlines, per kind when there are enough), `_templates` (numbered names used twice or more,
for casing and an alternative title), and `related` (IDF-weighted share of each item's title
found in the post; needs two shared words or a whole numbered title, and penalises a
different number, so "Lab 4" doesn't match a post about lab 5). To change its behaviour, run
it on sample posts (`draft({"description": …, "posted_at": …}, course_items)`) and compare
the drafts before and after; it has many interacting heuristics.

`api/commands.py` interprets `POST /extra/commands`: `interpret(command, item, now, usual)`
returns a `Plan` (field changes, a `summary` sentence for the person, `notes` on how anything
ambiguous was read) or UNDO/REDO, which the API resolves to the item's latest action in
effect (undo) or latest undo in effect (redo). Matching order matters: undo/redo, note
commands (before dates, so a note's text is never read as one), done/not done
(`status_plan`, shared with `PUT /items/{source}/{item_id}/status`: a scraped item gets a
`turned_in`/`assigned` mark only while its source disagrees, and the mark is cleared when
marked the source's way, unless `keep` (the explorer always sends it), which stores it anyway so
it holds if the source changes; 409 for announcements and materials), reset/remove the due date, shifts by an amount ("push it back a
day"; "in 3 days" is a date, not a shift), then a date/time. What isn't said is kept from the
item: a time alone keeps its date, a date alone its time. A time without am/pm (`_pick_half`):
1-6 and :59 are PM, 12 is noon, 7-11 whichever is nearer the item's current due time. A
command that changes nothing returns `action_id: null` and logs nothing. The API also takes a
`url` instead of `source`/`item_id` (`storage.find_item_by_url`: explorer links, Classroom ids
in the path, else stored `url`s matched like the explorer's resolver), so a phone shortcut
can post a copied link and a dictated command.

### Finishing a sign-in by hand

`deploy/reauth.py` (stdlib only, runs on the user's machine) is the one-command fix for a
login challenge on the server. It runs `docker exec <scraper> login.sh` over SSH. That
starts Xvfb, x11vnc on the container's loopback and a headed scrape. It then serves a
random `127.0.0.1` port locally, and pipes each VNC connection through `docker exec … python -c`
into the container, so nothing listens on any network. `login.sh` stops everything
when its stdin closes (`LIFEAPI_LOGIN_STOP_ON_EOF=1`), so Ctrl-C or a dropped SSH
connection never leaves a headed Chrome holding the profile lock. When docker gets
permission denied, it reruns through sudo: `sudo -n` if sudo needs no password, otherwise
`sudo -k -S` with a password asked for locally and written as the first line of each
command's stdin (SSH gives sudo no TTY to prompt on). `-k` makes sure sudo always consumes
that line, so it never ends up in the VNC stream. The container's `sh` is
dash: its `kill` rejects `--`, so process groups are killed with `kill -TERM "-$PGID"`.
`/sources` adds `login_command` (built from `LIFEAPI_REAUTH_TARGET`) when the last run
failed with `failure` `login` (a `LoginError`), and the Sync status page shows it under the error.

### Manual sync

The API can't scrape itself: in containers it runs in a different service without the
credentials. So `POST /sync` inserts a `sync_requests` row (`sources` JSON list, NULL for
every enabled source) and touches `config.SYNC_TRIGGER` (`data/sync-requested`). A waiting
request that already covers the sources is returned instead of a duplicate.

- Every `runner.run()`, under the run lock: fails requests left `running` by a run that
  died, deletes the trigger, then claims each pending request whose sources it fetches in
  full (a partial run claims nothing). If any stay pending, it re-touches the trigger.
  Deleting before reading means a request queued mid-run is never missed.
- `--requested` runs the union of pending requests' sources, and returns before opening
  the browser if there are none.
- Wakeups: launchd's `com.lifeapi.sync` job `WatchPaths` the trigger. The container's
  `scrape-loop.sh` polls it every `LIFEAPI_SYNC_POLL` seconds between scheduled runs.
  Python's `run.lock` is separate from `scrape.sh`'s `scrape.lock`, because the Python
  process inherits the shell's flocked fd, and taking the same file again would deadlock.

### Schedules

`PUT /sources/{source}/schedule` stores a `schedules` row: `interval_minutes`, and
optionally `partial` (a name from the source's `Source.partials`) with `full_every`. A
source without a row is fetched in full every `LIFEAPI_SCRAPE_INTERVAL` seconds
(`config.SCRAPE_INTERVAL_MINUTES`; the API reads it too, to report the default).

- `storage.next_fetch()` derives everything from `scrape_runs`, so there's no counter to
  keep in step: the next fetch is due `interval_minutes` after the last run's start (any
  run: scheduled, manual, partial, failed), and it's full unless fewer than
  `full_every - 1` runs followed the last full one. A source that never ran is due now, in
  full. `scrape_runs.partial` records which partial a run did (NULL: full).
- `--due` (`runner._due`) runs the enabled sources that are due, each as planned, and
  returns before opening the browser if none are. launchd's `com.lifeapi.scraper` and
  `scrape-loop.sh` run it every minute; "nothing due" logs at debug so it stays off the console.
- A partial fetch is `Source.scrape()` with `self.partial` set: it returns only part of
  the source's records, so `save_result(partial=True)` upserts and retires nothing.
  `/sources`' `last_success_at` counts full runs only. To add a partial, add it to the
  source's `partials` (name -> short description, shown in the explorer) and branch on
  `self.partial`; the API, CLI (`--partial`) and explorer pick it up.

### Headed sources and Cloudflare

Bot checks spot headless Chrome more easily than a headed one, so a source can run headed.
`Source.headed` is the default (True only for VHL). `PUT /sources/{source}/browser`
(`{"headed": bool}`) stores a `browser_settings` row that overrides it, and `DELETE`
restores the default. The explorer edits it in the Sync status page's Browser column.
`--headed` and `LIFEAPI_HEADLESS=0` make every source headed (`runner._headless`).

- `browser_context()` on Linux with no `DISPLAY` (the container) starts Xvfb
  (`browser.virtual_display`, `-displayfd` so there's no startup poll) and sets `DISPLAY`
  while that launch lasts, so Xvfb only runs while headed sources do. `login.sh` already
  sets `DISPLAY=:99`, so it shares that display. On macOS a headed source opens a real
  Chrome window. Running headed measured about +140 MB peak RAM and +18% CPU while pages
  load, with no change in wall time.
- `cloudflare.pass_challenge(page)` is a no-op unless the page is Cloudflare's "Just a
  moment..." interstitial (detected from the DOM: the challenge strips its token from the
  URL). It waits for the page to reload itself, and clicks Turnstile's checkbox if it
  appears. The click goes through `xdotool` when there's an X display, so it's a real
  pointer event, otherwise over CDP. patchright's locators reach the checkbox through
  Turnstile's iframe and shadow roots. If the page doesn't clear within
  `config.timeout(30_000)`, it saves a debug snapshot and raises `LoginError`, so
  `/sources` offers `reauth.py`. A clearance lasts as long as the site's Cloudflare
  settings allow, from the same IP.
- Turnstile replaces its iframe when it resets the widget, and the iframe is out of
  process, so a locator call in flight on the old one fails with `TargetClosedError`
  ("Target page, context or browser has been closed") while the page is fine.
  `pass_challenge` looks for the checkbox again then (`_widget_gone`). In a run's trail,
  tabs closing right after the error are the source's own `finally:` cleanup, not the cause.
- To test it without a real challenge, serve a page titled "Just a moment..." that embeds
  Turnstile with Cloudflare's test sitekey `3x00000000000000000000FF` (forces the
  checkbox) or `2x00000000000000000000AB` (never passes). Test keys work on localhost.
- The server's datacenter IP is challenged on every VHL page and never cleared (runs
  94–101), while VHL lets a home IP straight through. `LIFEAPI_PROXY` sends the hosts in
  `LIFEAPI_PROXY_DOMAINS` (default VHL and Turnstile) through an upstream proxy, by way of
  `proxy.relay()`, a local relay that `browser_context()` starts and points Chrome at
  with a PAC script; everything else stays direct. The proxy in use (2026-10-07)
  rotates the exit address per connection within an IPv6 /48 (Hurricane Electric),
  reaches IPv6 only, and answers 407 without `Proxy-Authenticate`, which Chrome can't
  answer. So the relay sends Basic credentials up front, and tunnels hosts with no IPv6
  address that are on Cloudflare (VHL) to their Cloudflare IPv6 twin. Cloudflare picks
  the site by SNI on any of its addresses. Other hosts without IPv6 (VHL's assets on
  CloudFront) go direct. Never log the proxy URL: it holds the password.

### Frontend (`frontend/`)

A temporary, deliberately unstyled explorer: plain semantic HTML, no CSS, no build step.
It's a user-facing wrapper (Today, Upcoming, Missing, Announcements, Comments, Courses, Grades,
Search, Sync status with sync buttons and schedule and browser editors; item pages with a
✎ button beside each editable field (due date, status, note, and a custom item's title, kind,
points and description) that opens a small form under it (`editable()`, `editors()`); "Convert
to assignment" on announcements, a form filled from `/extra/drafts`), not an endpoint browser.
Saving a due date or status stores it as the student's even when it's the source's own value
(the button says "Keep" then), so it stays if the source changes; "Use <source>'s" resets it.
After a save the page shows the logged change with Undo (`?did=`). Raw
API access stays at `/api/docs`.

- `serve.py` is stdlib only. It proxies `/api/*` (GET, POST, PUT, PATCH, DELETE; each method needs its
  own `do_<METHOD>`, or `BaseHTTPRequestHandler` answers 501) to the API, so the API needs no CORS.
  It sends `X-Forwarded-Prefix: /api`, which the API's `forwarded_prefix` middleware turns
  into the request's `root_path`. That way `/api/docs` loads `/api/openapi.json`, and the
  spec's `servers` is `/api`, so "Try it out" works. Of the API's response headers it passes
  on only `Content-Type`, so pages should read data from the body, not headers. Paths in `PAGES` serve standalone pages,
  paths in `MOVED` redirect to their new address, and every other path serves `index.html`. It re-reads
  pages on each request, so page edits need only a browser refresh. Changes to `serve.py` need a restart.
  `--host-page HOST=PAGE` serves a `PAGES` entry at `/` when the `Host` header (port ignored) is `HOST`.
  Compose uses it to put `/big/gpa` at the root of `gpa.stan.ke`, a second domain on the same service.
- `big.html` is the styled pages `/big/gpa`, `/big/missing`, `/big/next`, `/big/due` and
  `/big/status` (`/biggpa` redirects to `/big/gpa`): each shows `GET /api/json/<name>` in large
  Inter (Google Fonts), sized by `fit()` to the width and to the height left under the header,
  re-fetched every 3 minutes (a failed refresh keeps the last value). Since a `--host-page` serves
  one at `/`, the page can't tell which it is from its URL: `serve.py` fills `{{name}}` into
  `<html data-big>`. Each has its own colours, CSS variables set per `data-big` that the favicon
  and `theme-color` read too: GPA black on white, missing white on Swiss red, next black on Swiss
  Post yellow, due white on blue, status white on black. They send no token (`/json/*` needs
  none), so they work on any device. Missing, next and due pass `?days=` on to the API (7 if
  absent, as there) and show the window in the header. The line under the title is the age of
  `last_seen_at` (GPA) or `updated_at` (the rest), "Updated 2 h ago", preceded by the error when
  a refresh fails. On the GPA page the unit label is a `<button>` styled as plain header text:
  clicking it switches between the percentage and the 4.0 scale (the percentage / 25, still to
  three places), and the choice is kept in `localStorage`. The due page counts down to `due_at`
  itself ("3h 20m", "2d 4h"; "None" when nothing is due), with the item's title, course and
  deadline in a caption above it. `schedule()` re-arms one timer on every render: a minute later,
  or on the due page when the countdown's minute turns over (at the deadline's seconds), and at
  the deadline it fetches the next one. The tab title (`GPA - 99.150`, `Due - 3h 20m`) and favicon
  follow the shown value, so a background tab shows it: `renderIcon()` draws one centred row
  (the countdown's largest unit only), and the GPA's digits before the point over the three
  after it on a 64px canvas, right-aligned in three fixed columns (` 99` over `150`). Nothing may
  wait on `requestAnimationFrame` (hidden tabs never run it); timers are throttled to about once
  a minute there, and a frozen tab refreshes on `resume`.
- `index.html` holds all the JS in one inline script. A tiny `h(tag, attrs, ...kids)`
  helper builds the DOM. Views are async functions that return nodes, and the hash router
  calls them as `view(...pathArgs, params)`. Routes are `#/item/<source>/<id>`,
  `#/course/<source>/<id>`, `#/grade/<source>/<id>` and so on, so pages can be
  bookmarked. Views fetch in parallel. That relies on the API's `check_same_thread=False`
  (see above).
- Debug snapshots: a failed run's log box and the Sync status page's "Debug snapshots" list
  open files with `openDebugFile()`. It fetches with the token in a header and opens a `blob:`
  URL, so the token never goes in a URL. It opens the tab before the fetch, because a tab
  opened after an `await` gets popup-blocked.
- Display helpers live in one place: `SOURCE_NAMES` (add new sources there), `label()`
  for statuses, `relative()`/`when()` for dates, and `DONE`, which mirrors the API's
  finished statuses.
- **Commands**: `<site>/<item link>##<command>` (or a single `#` when the command has a space
  or is `undo`/`redo`/`done`/`reset`; `splitCommand`) is rewritten to `#/open?url=…&do=…`.
  `viewOpen` resolves the link and goes to `#/item/…?do=…`; `route()` runs `?do=` through
  `runPending` (POST /extra/commands) and replaces it in the address with `?did=<action>`
  (or `?msg=`, `?cmd_error=`), so a reload or Back never runs it twice. `commandBanner` shows
  `did`'s summary and notes with Undo/Redo above any view. Item pages end with a command box
  (it sets `?do=`) and the item's last commands and edits, each with Undo.
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
  Scrapers set `due_at` to the source's deadline; the API's `Item` (`api/schemas.py`)
  reports the effective one as `due_at` and the source's as `source_due_at`.
- Anything source-specific goes in `extra`. For Classroom, `extra.submitted_work` is the
  student's own attachments, `extra.links` are links from the description,
  `extra.list_signature`, `extra.detail_fetched_at` and (announcements) `extra.read_at`
  are cache bookkeeping.
- `Comment.posted_at` is the source's display text ("10:42 AM", "Sep 18"), which is relative
  to when it was read, so the scraper also stores `posted_time`, parsed then
  (`google_classroom.comments()`). `GET /comments` and the explorer's Comments page sort and
  filter by it. Comments cached before ids and `posted_time` were kept have no `id`: detail
  pages get `posted_time` from `detail_fetched_at` on merge, and announcements re-read them.
- Infinite Campus produces `Grade` records (one per course × term × grading task), each
  with `GradeEntry` assignment scores, plus one course-less `Grade` per overall GPA
  (`gpa` set, id `gpa:<calendarID>:<type>:<termSeq>:<w|uw>`). It produces no `Item`s.
- GPAs are published to three decimal places (`models.GPA_PLACES`). Stored as floats, they
  lose trailing zeros (`99.150` is stored as `99.15`), so anything that turns a GPA into text
  pads it to three places: `/min/gpa` and `/json/gpa` write their bodies by hand, `/big/gpa` and the explorer's
  `gpaText()` format it.
- `status` keeps each platform's own wording, in snake_case.

## Exploring a site's structure

The approach that worked: write throwaway scripts in `data/dev/` (gitignored) that
reuse `browser_context()` and the auth helpers, save HTML and screenshots with
`dump_debug`, and log XHR/fetch responses to find JSON endpoints. Don't log request
bodies on login pages (see Credentials). Delete `data/dev/` afterwards.
