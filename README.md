# lifeapi

Collects schoolwork from every platform into one SQLite database and serves it as a
read-only HTTP API.

The project has two halves that run separately:

- **Scraper** (`python -m lifeapi.scraper`): a stealth Chrome
  ([patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)), headless
  except for sources behind bot checks ([Headed sources](#headed-sources)), signs in to each
  platform and writes to `data/lifeapi.db`. Run `--due` every minute and it fetches each
  source on its own schedule (every 2 hours unless set otherwise through the API).
- **API** (`python -m lifeapi.api`): FastAPI, read-only over the same database except
  for queueing manual syncs (`POST /sync`), which the scraper picks up. Leave it running.

| Source | Key | Login | What's collected |
|---|---|---|---|
| Google Classroom | `google_classroom` | Google | Assignments, questions, quizzes and materials (from Classwork), plus announcements (from Stream). Includes description, due date, points, score, status, grading category, topic, attachments, links, submitted work, and class and private comments. |
| AP Classroom | `ap_classroom` | College Board | AP Daily videos (watch %) and assessments (progress, score/max, timer, results link) for every AP subject. |
| Vista Higher Learning | `vhl` | Google via Clever | Assignment groups by due date, with activities completed/assigned, status, estimated time and launch link. |
| Infinite Campus | `infinite_campus` | Google SSO | Grades only: each course's grade for every term and grading task, category breakdown, and every graded assignment with its score, flags (missing, late, …) and link. |
| Albert | `albert` | Google | Stub, disabled until Albert has assignments. |

Every record carries a `url` that deep-links back to the original page.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/patchright install chromium   # only needed if Google Chrome isn't installed
```

Put the credentials in `.env` in the project root:

```
GOOGLE_USERNAME=...
GOOGLE_PASSWORD=...
COLLEGEBOARD_USERNAME=...
COLLEGEBOARD_PASSWORD=...
```

Optional settings:

| Variable | Default | |
|---|---|---|
| `LIFEAPI_HEADLESS` | `1` | `0` shows the browser window for every source. Otherwise each source follows its own setting (see [Headed sources](#headed-sources)). |
| `LIFEAPI_BROWSER_CHANNEL` | `chrome` | Uses the installed Google Chrome. Set it to empty to use patchright's Chromium instead. |
| `LIFEAPI_TIMEOUT_SCALE` | `3` | Multiplies the scraper's wait deadlines (page loads, selectors, logins). Raise it on a slow host. |
| `LIFEAPI_SOURCE_TIMEOUT` | `1800` | Seconds one source's scrape may take. A source still running after that is stopped and recorded as failed, so a hung page can't hold up the others. |
| `LIFEAPI_SCRAPE_INTERVAL` | `7200` | Seconds between fetches of a source with no schedule set (see [Schedules](#schedules-and-partial-fetches)). The API reads it too, to report schedules, so give both the same value. |
| `LIFEAPI_API_TOKEN` | unset | If set, the API requires the token (`Authorization: Bearer <token>`, or `?token=<token>`) on everything except `/health`, `/min/…` and `/json/…`. |
| `LIFEAPI_REAUTH_TARGET` | unset | The server's SSH destination (e.g. `root@vps`), or `--local`. Fills in the sign-in command that `/sources` shows when a login gets stuck. See [Finishing a sign-in challenge](#finishing-a-sign-in-challenge). |
| `LIFEAPI_DATA_DIR` | `./data` | Holds the DB, the browser profile and debug snapshots. |
| `CLEVER_PORTAL_URL`, `INFINITE_CAMPUS_URL` | Jersey City | District-specific URLs. |

## Running

```sh
.venv/bin/python -m lifeapi.scraper                  # every enabled source
.venv/bin/python -m lifeapi.scraper --only vhl infinite_campus
.venv/bin/python -m lifeapi.scraper --headed         # watch it
.venv/bin/python -m lifeapi.scraper --list
.venv/bin/python -m lifeapi.scraper --requested      # only what POST /sync has queued
.venv/bin/python -m lifeapi.scraper --due            # only what's due on its schedule
.venv/bin/python -m lifeapi.scraper --only infinite_campus --partial gpa   # a partial fetch

.venv/bin/python -m lifeapi.api --port 8000          # docs at http://127.0.0.1:8000/docs
```

Each source runs on its own. If one fails (a login challenge, or a site redesign), the
others still update, the failed source keeps its last good data, and `/sources` shows the
error. When a page doesn't look as expected, the scraper saves a screenshot and the HTML to
`data/debug/`. The API serves them (`GET /debug`), and the explorer's Sync status page links
to them.

**First run and login challenges.** Sessions are kept in `data/browser-profile/`, so the
scraper rarely has to sign in from scratch. If Google or College Board ever asks for extra
verification, run `python -m lifeapi.scraper --headed --only <source>` once, finish the
sign-in in the window, and scheduled runs will work again. The first Google Classroom run
opens every item's detail page and takes about 15 minutes. After that, each run re-reads
only items that are new, changed, recent (last 21 days) or not refreshed in the last 24
hours.

## Scheduling (macOS)

```sh
cp deploy/com.lifeapi.*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.lifeapi.scraper.plist   # every minute, runs what's due
launchctl load ~/Library/LaunchAgents/com.lifeapi.api.plist       # always on, :8000
launchctl load ~/Library/LaunchAgents/com.lifeapi.sync.plist      # runs POST /sync requests
```

The plists contain absolute paths to this checkout. Logs go to `data/scraper.log` and
`data/api.log`. Only one scraper can use the browser profile at a time. A run that starts
while another is going waits for it (`data/run.lock`), so manual runs and scheduled ones
queue up instead of colliding. On Linux, the cron equivalent is
`* * * * * cd /path/to/lifeapi && .venv/bin/python -m lifeapi.scraper --due`, plus
`* * * * * cd /path/to/lifeapi && test -e data/sync-requested && .venv/bin/python -m lifeapi.scraper --requested`
for manual syncs. If you installed the plists before schedules existed, copy and reload
`com.lifeapi.scraper.plist` again: the old one runs every source in full every 2 hours.

### Schedules and partial fetches

Each source has a schedule: how often to fetch it, and optionally a cheaper partial fetch
to do in between full ones. Infinite Campus has one partial fetch, `gpa`, which reads only
the overall GPA. To read the GPA every 30 minutes and every grade every 2 hours:

```sh
curl -X PUT http://127.0.0.1:8000/sources/infinite_campus/schedule \
  -H 'Content-Type: application/json' \
  -d '{"interval_minutes": 30, "partial": "gpa", "full_every": 4}'
```

Every 4th fetch is then full and the other three are `gpa`. `DELETE` on the same path goes
back to the default (in full every `LIFEAPI_SCRAPE_INTERVAL`). The explorer's Sync status
page has the same controls ("change" under each schedule), and shows when each source is
next fetched. The interval counts from the start of the source's last run, scheduled or
manual. A partial fetch only adds and updates records; nothing is marked inactive until
the next full one.

### Headed sources

VHL Central is behind Cloudflare, which challenges visitors it doesn't trust, such as a
server's datacenter IP. Headless Chrome is easier for it to spot, so VHL runs in a headed
(visible) browser by default, and every other source runs headless. To change a source:

```sh
curl -X PUT http://127.0.0.1:8000/sources/vhl/browser \
  -H 'Content-Type: application/json' -d '{"headed": false}'
```

`DELETE` on the same path restores the source's default. The explorer's Sync status page
has the same control (the Browser column). The scraper runs headed sources in a browser
launch of their own after the headless ones. On a Linux host with no display (the
container), it runs that browser on a virtual display (Xvfb, which it starts and stops
itself; outside the container, install `xvfb` and `xdotool`). On macOS, a Chrome window appears while a headed source runs. Headed costs about
140 MB more memory at peak, and a bit more CPU while pages load.

When a page shows Cloudflare's "Just a moment..." challenge, the scraper waits for it to
pass and clicks its "Verify you are human" checkbox if it asks. If it still doesn't pass,
the run fails with a sign-in error, and the Sync status page shows the
[`reauth.py`](#finishing-a-sign-in-challenge) command to finish it by hand. If it keeps
happening, the server's IP is the likely reason, and only a different network fixes that.

### Syncing on demand

`POST /sync` (all enabled sources) or `POST /sync?source=vhl&source=infinite_campus`
queues a sync, and the explorer's Sync status page has buttons for it. The API records the
request in the database and touches `data/sync-requested`. The `com.lifeapi.sync` job
watches that file and runs `python -m lifeapi.scraper --requested`. In containers, the
scraper loop checks for it every `LIFEAPI_SYNC_POLL` seconds. If a run is already going, the
request waits for it to finish. A scheduled run that fully fetches a request's sources
settles it too. Without the sync job or the container loop, requests stay `pending` until the
next scheduled run.

## Containers (Coolify, podman)

`docker-compose.yaml` runs three services from one image (`Dockerfile`):

| Service | What it runs |
|---|---|
| `api` | The API on port 8000, inside the stack's network only. |
| `frontend` | The explorer on port 8080. It proxies `/api/*` to `api`, so it's the only service that needs a public domain. The API docs are at `/api/docs`. |
| `scraper` | Every minute, runs the sources that are due on their [schedules](#schedules-and-partial-fetches) (by default every `LIFEAPI_SCRAPE_INTERVAL` seconds, 7200), so at startup it fetches whatever is overdue. In between, it runs `POST /sync` requests within `LIFEAPI_SYNC_POLL` seconds (default 10). |

They share the `data` volume, which holds the DB, the browser profile and debug snapshots.
On amd64 the image installs Google Chrome. On arm64 (podman on Apple Silicon) it installs
patchright's Chromium instead, because Chrome isn't published for Linux arm64.

Environment variables: `GOOGLE_USERNAME`, `GOOGLE_PASSWORD`, `COLLEGEBOARD_USERNAME`,
`COLLEGEBOARD_PASSWORD` and `LIFEAPI_API_TOKEN` are required. Compose refuses to start
without them. `LIFEAPI_SCRAPE_INTERVAL`, `LIFEAPI_SCRAPE_MAX_RUN`, `LIFEAPI_SOURCE_TIMEOUT`, `LIFEAPI_SYNC_POLL`, `LIFEAPI_TIMEOUT_SCALE`,
`CLEVER_PORTAL_URL`, `INFINITE_CAMPUS_URL` and `LIFEAPI_REAUTH_TARGET` are optional.
`LIFEAPI_PROXY` (`http://user:password@host:port`, optional) sends the sites in
`LIFEAPI_PROXY_DOMAINS` (default `vhlcentral.com,challenges.cloudflare.com`) through a proxy,
for when their bot checks challenge the server's IP. VHL's Cloudflare does that to a
datacenter address. Everything else, sign-ins included, stays direct.

Every service has a healthcheck. `api` and `frontend` are checked over HTTP, and
`frontend` waits for `api` to be healthy. `scraper` turns unhealthy only when a run hangs
past `LIFEAPI_SCRAPE_MAX_RUN` seconds (default 3600). A source that fails doesn't count;
those failures show up in `/sources`. The API token is required because the deployment is public. Enter it in the
explorer's "API token" field.

**Locally with podman:**

```sh
podman compose up -d --build          # reads .env; also merges docker-compose.override.yaml
podman compose logs -f scraper
open http://127.0.0.1:8080
```

The override publishes the API on `127.0.0.1:8000` and the explorer on `127.0.0.1:8080`.
The container has its own browser profile, separate from
`data/browser-profile/` on the Mac.

**On Coolify:**

1. Create a new resource from the GitHub repository (branch `main`) and choose the
   **Docker Compose** build pack, with compose file `/docker-compose.yaml`. Coolify builds the
   image on the server, so no CI is needed. It runs the file with `-f`, so
   `docker-compose.override.yaml` is ignored.
2. Under Environment Variables, set the required variables listed above.
3. Give the `frontend` service a domain with the container port, for example
   `https://lifeapi.example.com:8080`. Leave `api` and `scraper` without a domain.
   A second domain on `frontend` can show the big GPA page at its root: list both
   (comma-separated) and add `--host-page <domain>=/biggpa` to its command in
   `docker-compose.yaml`.
4. Deploy. Watch the `scraper` logs for the first run. The first Google Classroom run
   reads every detail page and takes about 15 minutes.

### Finishing a sign-in challenge

A Chrome profile from macOS can't be copied over, because its cookies are encrypted with
the Mac's Keychain. The server signs in from scratch, from an address Google and College
Board haven't seen before, and they may ask for extra verification (for Google, a
`.../signin/confirmidentifier` page). When that happens, the explorer's Sync status page
shows the fix under the source's error, ready to copy. From a checkout of this repository
on your own machine:

```sh
python3 deploy/reauth.py root@your-server --only google_classroom
python3 deploy/reauth.py --local --only google_classroom    # containers on this machine
```

It finds the scraper container over SSH, starts a headed scrape in it on a virtual display
(`deploy/container/login.sh`), and opens the browser window in Screen Sharing (on other
systems it prints a `127.0.0.1` port for any VNC viewer). Finish the sign-in there. The
scrape then carries on, and the session is saved in the `data` volume for scheduled runs.
If a scheduled run is going, it waits for it first. Ctrl-C stops the session on the server.
It needs only Python 3 and SSH access to the server. If your SSH user isn't in the `docker`
group, it switches to `sudo docker` when docker refuses permission, and asks for your sudo
password unless sudo needs none (`--sudo` skips the first try). Pass `--container NAME` if
it can't find the container.

Set `LIFEAPI_REAUTH_TARGET` on the `api` service to your SSH destination so the command on
the Sync status page is complete. Without it, the page shows `<user@server>`.

Security:

- VNC listens only on the scraper container's loopback. No port is published on the
  server or the container network. `reauth.py` pipes each VNC connection through
  `docker exec` over your SSH login, so reaching the browser takes the same access as
  running commands on the server.
- On your machine it listens on `127.0.0.1` only, on a random port.
- The VNC password is random for each session and is handed to x11vnc in a file that's
  deleted at once, never on a command line.
- When `reauth.py` exits or its SSH connection drops, the scrape, display and VNC server
  stop.
- The command (with your SSH destination) is part of `/sources`, so it's behind the API
  token, like the rest of the data.

## API

All list endpoints return only items still present at the source, unless you pass
`include_inactive=true`. Items deleted at the source are kept and marked `"active": false`.

| Endpoint | |
|---|---|
| `GET /items` | Everything. Filters: `source`, `kind` (`assignment`, `quiz`, `question`, `material`, `announcement`; repeatable), `status`, `course_id`, `due_after`, `due_before`, `posted_after`, `q`, `order=due\|posted\|seen`, `limit`, `offset`. |
| `GET /items/upcoming?days=14` | Work due soon that isn't done. |
| `GET /items/missing` | Past due and not done. |
| `GET /announcements?days=14` | Recent announcements. |
| `GET /items/{source}/{id}` | One item. |
| `GET /courses` | Classes per source. |
| `GET /grades` | Infinite Campus grades. Filters: `source`, `term`. |
| `GET /history` | Every change to grades, GPAs, category totals, assignment scores and item statuses/scores, newest first, kept for good. Filters: `source`, `kind` (`grade`, `entry`, `item`), `id` (a grade's id includes its assignments), `gpa=true`, `since`, `limit`. |
| `GET /min/{name}`, `GET /json/{name}` | One number, as plain text (`/min`) or JSON `{"value": …}` (`/json`). Needs no token. `name` is `gpa` (cumulative weighted GPA, a percentage always written to three decimal places, e.g. `99.150`, that weighting can lift above 100; the JSON adds `last_seen_at`), `missing` (items from `/items/missing` due in the last `?days=`, default 7), `next` (unfinished items due in the next `?days=`, default 7) or `status` (enabled sources whose last run failed; the JSON adds `minutes`, the age of the stalest source's last successful full run). |
| `GET /sources` | Every source, whether it's enabled, its partial fetches, its schedule and next fetch, and its last run (`null` if never): when it ran, whether it was partial, whether it succeeded, the error, and counts. |
| `PUT /sources/{source}/schedule` | Set how often a source is fetched: JSON `{"interval_minutes": 30}`, optionally with `"partial"` and `"full_every"`. See [Schedules](#schedules-and-partial-fetches). |
| `DELETE /sources/{source}/schedule` | Back to the default schedule. |
| `POST /sync` | Queue a sync now. `source` (repeatable) limits it; omit for every enabled source. Returns the request (202, or 200 if a waiting request already covers it). See [Syncing on demand](#syncing-on-demand). |
| `GET /sync` | Recent sync requests, newest first. `status`: `pending`, `running`, `done` or `failed` (with `error`). |
| `DELETE /sync` | Clear the sync request list: deletes finished requests and cancels waiting ones. |
| `GET /sync/{request_id}` | One sync request. |
| `GET /runs`, `GET /runs/{run_id}` | Recent scrape runs, and one run with its log and the debug snapshots it saved (failed runs only). |
| `GET /debug` | Debug snapshots: the screenshot and HTML the scraper saved when a run failed on a page it didn't expect, newest first. Each is replaced by the next with the same name. |
| `GET /debug/{file}` | One snapshot file: `<name>.png` as an image, `<name>.html` as plain text (so the captured page's scripts never run on this site). |
| `GET /health` | Liveness check. |
| `GET /db` | The whole SQLite database as a file (a consistent snapshot, safe while the scraper runs). To copy the server's data to this machine, stop the local scraper and API, then: `curl -fH "Authorization: Bearer $TOKEN" https://<server>/api/db -o data/lifeapi.db.new && rm -f data/lifeapi.db-wal data/lifeapi.db-shm && mv data/lifeapi.db.new data/lifeapi.db` |

An item looks like this:

```json
{
  "source": "google_classroom", "id": "234567890123", "kind": "assignment",
  "title": "APV 3 (Unit 1.5 and 1.6)",
  "url": "https://classroom.google.com/u/0/c/MTIzNDU2Nzg5MDEy/a/MjM0NTY3ODkwMTIz/details",
  "course_id": "123456789012", "course_name": "AP PreCalculus - Period 2",
  "description": "AP Videos (APV) ...", "author": "Teacher Name",
  "posted_at": "2026-10-02T00:00:00-04:00", "due_at": "2026-10-09T08:00:00-04:00",
  "due_text": "Due Oct 9, 8:00 AM", "status": "assigned",
  "points_possible": 30.0, "score": null,
  "attachments": [], "comments": [],
  "extra": {"topic": "AP Classroom Weekly Projects", "category": "Projects"},
  "active": true, "first_seen_at": "...", "last_seen_at": "..."
}
```

For Google Classroom, `attachments` holds only what the teacher attached. Your own work
(uploads, and the copy of a template the teacher made for you) is in
`extra.submitted_work`. Links found in the description are in `extra.links`.

`status` uses each platform's own wording, in snake_case: `assigned`, `missing`,
`turned_in`, `turned_in_late`, `graded`, `returned`, `completed`, `upcoming`, `partial`,
`partial_late`. Times are ISO 8601. Google Classroom only shows dates like "Sep 16", so
when there's no time of day, the year is inferred and the time is set to 23:59 for due
dates and 00:00 for posted dates.

## Adding a platform

1. Create `lifeapi/scraper/sources/<name>.py`. Subclass `Source`, set `name`, implement
   `async def scrape(self) -> ScrapeResult`, and decorate it with `@register`.
2. Import it in `lifeapi/scraper/sources/__init__.py`.
3. Reuse a sign-in from `lifeapi/scraper/auth/`: `google_login`, `collegeboard_login`, or
   `clever.launch_app(context, page, "<tile name>")` for anything on the Clever dashboard.

Useful pieces on `Source`:

- `self.context`: the shared, already-signed-in browser context.
- `self.previous`: the items this source produced on its last run, so you can skip pages
  that haven't changed.
- `self.map_pages(items, fn)`: runs page visits in parallel across a few tabs.
- `scraper/dates.py`: parses dates the way sites display them.

If a site's frontend loads JSON (as AP Classroom, VHL and Infinite Campus do), read that
JSON instead of the DOM. It's far more stable.

## Notes and limitations

- **Google Classroom** CSS class names are minified and change between releases, so the
  extractors (`sources/google_classroom_js.py`) rely on `data-*` attributes, ARIA labels
  and text patterns instead. If Google redesigns a page, that file is the one to fix.
  Only classes you're enrolled in are scraped (not ones you teach), and hidden or archived
  classes are skipped.
- **VHL** doesn't open activities, because opening one starts it. Items are the assignment
  groups VHL shows for each due date, not individual activities. VHL announcements aren't
  collected yet: the panel was empty, so there was nothing to build a parser against.
- **AP Classroom** video links use AP Daily's `apclassroom.collegeboard.org/d/<code>`
  short-link format. Assessments that are still open link to the subject's assignment
  list, because AP Classroom has no stable per-assessment URL before you start one.
- **Infinite Campus**: as requested, only grades are collected (not IC's assignment list
  or attendance).
- **Retention.** Courses, items and grades are never deleted, only marked inactive, but each
  holds only its latest values. Every change to a grade-related value (grade letters and
  percents, GPAs, category totals, assignment scores, item statuses and scores) is also kept in
  `history` (`GET /history`) for good, which costs a few KB a week. Scrape runs and finished sync
  requests are deleted after 180 days, and failed-run logs are kept for each source's last 20
  runs. Compose rotates each container's log at 10 MB × 3.
