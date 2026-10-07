# lifeapi

Collects schoolwork from every platform into one SQLite database and serves it as a
read-only HTTP API.

The project has two halves that run separately:

- **Scraper** (`python -m lifeapi.scraper`): a stealth headless Chrome
  ([patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)) signs in to each
  platform and writes to `data/lifeapi.db`. Run it on a schedule (every 2 hours).
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
| `LIFEAPI_HEADLESS` | `1` | `0` shows the browser window. |
| `LIFEAPI_BROWSER_CHANNEL` | `chrome` | Uses the installed Google Chrome. Set it to empty to use patchright's Chromium instead. |
| `LIFEAPI_TIMEOUT_SCALE` | `3` | Multiplies the scraper's wait deadlines (page loads, selectors, logins). Raise it on a slow host. |
| `LIFEAPI_API_TOKEN` | unset | If set, the API requires `Authorization: Bearer <token>` on everything except `/health` and `/gpa`. |
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

.venv/bin/python -m lifeapi.api --port 8000          # docs at http://127.0.0.1:8000/docs
```

Each source runs on its own. If one fails (a login challenge, or a site redesign), the
others still update, the failed source keeps its last good data, and `/sources` shows the
error. When a page doesn't look as expected, the scraper saves a screenshot and the HTML to
`data/debug/`.

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
launchctl load ~/Library/LaunchAgents/com.lifeapi.scraper.plist   # every 2 h
launchctl load ~/Library/LaunchAgents/com.lifeapi.api.plist       # always on, :8000
launchctl load ~/Library/LaunchAgents/com.lifeapi.sync.plist      # runs POST /sync requests
```

The plists contain absolute paths to this checkout. Logs go to `data/scraper.log` and
`data/api.log`. Only one scraper can use the browser profile at a time. A run that starts
while another is going waits for it (`data/run.lock`), so manual runs and scheduled ones
queue up instead of colliding. On Linux, the cron equivalent is
`0 */2 * * * cd /path/to/lifeapi && .venv/bin/python -m lifeapi.scraper`, plus
`* * * * * cd /path/to/lifeapi && test -e data/sync-requested && .venv/bin/python -m lifeapi.scraper --requested`
for manual syncs.

### Syncing on demand

`POST /sync` (all enabled sources) or `POST /sync?source=vhl&source=infinite_campus`
queues a sync, and the explorer's Sync status page has buttons for it. The API records the
request in the database and touches `data/sync-requested`. The `com.lifeapi.sync` job
watches that file and runs `python -m lifeapi.scraper --requested`. In containers, the
scraper loop checks for it every `LIFEAPI_SYNC_POLL` seconds. If a run is already going, the
request waits for it to finish. A scheduled run that covers a request's sources settles
it too. Without the sync job or the container loop, requests stay `pending` until the
next scheduled run.

## Containers (Coolify, podman)

`docker-compose.yaml` runs three services from one image (`Dockerfile`):

| Service | What it runs |
|---|---|
| `api` | The API on port 8000, inside the stack's network only. |
| `frontend` | The explorer on port 8080. It proxies `/api/*` to `api`, so it's the only service that needs a public domain. The API docs are at `/api/docs`. |
| `scraper` | A scrape at startup, then one every `LIFEAPI_SCRAPE_INTERVAL` seconds (default 7200). In between, it runs `POST /sync` requests within `LIFEAPI_SYNC_POLL` seconds (default 10). |

They share the `data` volume, which holds the DB, the browser profile and debug snapshots.
On amd64 the image installs Google Chrome. On arm64 (podman on Apple Silicon) it installs
patchright's Chromium instead, because Chrome isn't published for Linux arm64.

Environment variables: `GOOGLE_USERNAME`, `GOOGLE_PASSWORD`, `COLLEGEBOARD_USERNAME`,
`COLLEGEBOARD_PASSWORD` and `LIFEAPI_API_TOKEN` are required. Compose refuses to start
without them. `LIFEAPI_SCRAPE_INTERVAL`, `LIFEAPI_SCRAPE_MAX_RUN`, `LIFEAPI_SYNC_POLL`, `LIFEAPI_TIMEOUT_SCALE`,
`CLEVER_PORTAL_URL`, `INFINITE_CAMPUS_URL` and `LIFEAPI_REAUTH_TARGET` are optional.

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
| `GET /gpa` | The cumulative weighted GPA as a bare number, a percentage (e.g. `99.15`). Weighting can lift it above 100. Needs no token. |
| `GET /sources` | Every source, whether it's enabled, and its last run (`null` if never): when it ran, whether it succeeded, the error, and counts. |
| `POST /sync` | Queue a sync now. `source` (repeatable) limits it; omit for every enabled source. Returns the request (202, or 200 if a waiting request already covers it). See [Syncing on demand](#syncing-on-demand). |
| `GET /sync` | Recent sync requests, newest first. `status`: `pending`, `running`, `done` or `failed` (with `error`). |
| `DELETE /sync` | Clear the sync request list: deletes finished requests and cancels waiting ones. |
| `GET /sync/{request_id}` | One sync request. |
| `GET /health` | Liveness check. |

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
