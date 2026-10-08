"""HTTP API over the scraped data. Runs independently of the scraper and only reads the
SQLite database the scraper writes, except for queueing and clearing manual sync requests
(`/sync`) and setting fetch schedules (`/sources/{source}/schedule`) and browser settings
(`/sources/{source}/browser`), which the scraper picks up, and the student's own additions:
notes, deadlines and statuses on items (`/items/{source}/{item_id}/note`, `/due`, `/status`)
and assignments made
from announcements (`/extra/...`), in tables the scraper never writes.

The OpenAPI spec is meant to stand on its own (handed to a person or an agent without the
code), so keep summaries, descriptions and `schemas.py` in step with behavior."""

from __future__ import annotations

import inspect
import json
import os
import secrets
import shlex
import sqlite3
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import APIKeyQuery, HTTPAuthorizationCredentials, HTTPBearer
from starlette.background import BackgroundTask

from .. import config, storage
from ..models import GPA_PLACES, ItemKind
from ..models import Item as ItemRecord
from ..scraper import sources as _sources  # noqa: F401  (registers all sources)
from ..scraper.base import REGISTRY, Source
from ..scraper.dates import LOCAL_TZ
from . import commands, drafts
from .schemas import (
    DESCRIPTION, DONE_STATUSES, TAGS, Action, AssignmentEdit, Browser, BrowserSettings,
    ClearedSyncRequests, CommandRequest, CommandResult, ConversionDraft, CountValue, Course,
    DueSettings, Error, GpaValue, Grade, Health, HistoryRecord, Item, NewAssignment, NoteSettings,
    Run, RunDetail, Schedule, ScheduleSettings, SourceStatus, StatusSettings, StatusValue,
    SyncRequest, errors,
)

app = FastAPI(
    title="lifeapi",
    summary="Schoolwork from every platform, in one place.",
    description=DESCRIPTION,
    version="1.0.0",
    openapi_tags=TAGS,
)


@app.middleware("http")
async def forwarded_prefix(request: Request, call_next):
    # Behind frontend/serve.py the API lives under /api, and the proxy says so in this
    # header. As the request's root_path it makes /docs fetch /api/openapi.json and the spec
    # list /api as its server, so "Try it out" calls /api/<path>. Direct access is unaffected.
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    if prefix.startswith("/"):
        request.scope["root_path"] = prefix
    return await call_next(request)

_bearer = HTTPBearer(
    auto_error=False,
    description="Required only when the server sets `LIFEAPI_API_TOKEN`.",
)


_query_token = APIKeyQuery(
    name="token",
    auto_error=False,
    description="The same token as the `token` query parameter, for clients that can't set "
                "headers. Required only when the server sets `LIFEAPI_API_TOKEN`.",
)


def require_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Depends(_query_token),
) -> None:
    if not config.API_TOKEN:
        return
    expected = config.API_TOKEN.encode()
    given = [t for t in (creds and creds.credentials, token) if t]
    if not any(secrets.compare_digest(t.encode(), expected) for t in given):
        raise HTTPException(401, "Missing or invalid token")


_schema_ready = False


def db() -> Iterator[sqlite3.Connection]:
    global _schema_ready
    if not config.DB_PATH.exists():
        raise HTTPException(503, "No data yet; run the scraper first")
    if not _schema_ready:
        # Adds tables and columns newer than the scraper that last opened the database, so
        # reads work right after an upgrade, before the scraper has run.
        storage.init_db()
        _schema_ready = True
    with storage.connect(readonly=True) as conn:
        yield conn


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


Auth = Depends(require_token)
READ_ERRORS = errors(401, 503)
SOURCE_NAMES = ", ".join(f"`{s}`" for s in sorted(REGISTRY))


def _source_param(repeatable: bool = False) -> Any:
    more = " Repeat to match any of several." if repeatable else ""
    return Query(None, description=f"Only records from this source: {SOURCE_NAMES}.{more}",
                 examples=["google_classroom"])


INCLUDE_INACTIVE = Query(False, description="Also return records that no longer appear on "
                                            "their source (`active: false`).")


def _login_command(source: str, run: dict[str, Any] | None) -> str | None:
    """The command that finishes a stuck sign-in by hand (deploy/reauth.py), for a run that
    failed on a login challenge. Run it from a checkout of this repository."""
    if not run or run["failure"] != "login":
        return None
    target = shlex.quote(config.REAUTH_TARGET) if config.REAUTH_TARGET else "<user@server>"
    return f"python3 deploy/reauth.py {target} --only {shlex.quote(source)}"


@app.get("/health", tags=["status"], operation_id="health", summary="Liveness check")
def health() -> Health:
    """Always `{"status": "ok"}` while the API process is up. Needs no token and says nothing
    about data freshness; use `GET /sources` for that."""
    return {"status": "ok"}


def _schedule(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """`name`'s effective schedule and its next fetch (see `Schedule`)."""
    cls = REGISTRY.get(name)
    schedule = storage.get_schedule(conn, name, cls.partials if cls else ())
    at, partial = storage.next_fetch(conn, name, schedule)
    if not (cls and cls.enabled):
        return {**schedule, "next_fetch_at": None, "next_fetch_partial": None}
    return {**schedule, "next_fetch_at": _iso(at) if at else storage.now_iso(),
            "next_fetch_partial": partial}


def _browser(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """`name`'s effective browser setting (see `Browser`)."""
    cls = REGISTRY.get(name)
    return storage.get_browser(conn, name, cls.headed if cls else False)


@app.get("/sources", dependencies=[Auth], tags=["status"], operation_id="listSources",
         summary="List sources, their schedules and last scrape", responses=READ_ERRORS)
def sources(conn: sqlite3.Connection = Depends(db)) -> list[SourceStatus]:
    """Every source with its fetch schedule, its browser setting, its most recent scrape run
    (null if it has never run) and when it last succeeded. Check `last_success_at` to see
    how fresh a source's data is; a failed run keeps the previous data, so a source can be
    stale even though it returns records. `login_command` is set when the last run got stuck on a sign-in challenge that a
    human has to finish. A failed run with `has_log` has a step-by-step log at
    `GET /runs/{run_id}/log`."""
    rows = {r["source"]: r for r in conn.execute(
        """SELECT r.* FROM scrape_runs r
           JOIN (SELECT source, MAX(run_id) AS run_id FROM scrape_runs GROUP BY source) last
             USING (source, run_id)"""
    )}
    out = []
    for name in sorted(REGISTRY.keys() | rows.keys()):
        r = rows.get(name)
        ok = conn.execute(
            "SELECT finished_at FROM scrape_runs WHERE source=? AND ok=1 AND partial IS NULL "
            "ORDER BY run_id DESC LIMIT 1",
            (name,),
        ).fetchone()
        partials = REGISTRY[name].partials if name in REGISTRY else {}
        last = r and storage.run_to_dict(r)
        out.append({
            "source": name,
            "enabled": name in REGISTRY and REGISTRY[name].enabled,
            "partials": [{"name": k, "description": v} for k, v in partials.items()],
            "schedule": _schedule(conn, name),
            "browser": _browser(conn, name),
            "last_run": last,
            "last_success_at": ok["finished_at"] if ok else None,
            "login_command": _login_command(name, last),
        })
    return out


# The schedule and browser endpoints write, so like /sync they open their own read-write
# connection.

SOURCE_PATH = Path(description=f"Source name: {SOURCE_NAMES}.", examples=["infinite_campus"])
SOURCE_ERRORS = {404: {"model": Error, "description": "No source with that name."}}


def _source(name: str) -> type[Source]:
    if name not in REGISTRY:
        raise HTTPException(404, f"Unknown source {name!r}. Available: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name]


@app.put("/sources/{source}/schedule", dependencies=[Auth], tags=["status"],
         operation_id="setSchedule", summary="Set how often a source is fetched",
         responses={**errors(400, 401), **SOURCE_ERRORS})
def set_schedule(settings: ScheduleSettings, source: str = SOURCE_PATH) -> Schedule:
    """Fetch `source` every `interval_minutes`. With `partial` and `full_every`, every
    `full_every`th fetch is full and the others do only that partial fetch (one of the
    source's `partials` in `GET /sources`). For example, `{"interval_minutes": 30, "partial":
    "gpa", "full_every": 4}` on `infinite_campus` reads the GPA every 30 minutes and every
    grade every 2 hours. The interval counts from the start of the last fetch, so a shorter
    one can make the source due at once. Returns the new schedule."""
    cls = _source(source)
    if settings.partial is not None and settings.partial not in cls.partials:
        raise HTTPException(400, f"{source} has no partial fetch {settings.partial!r}. Available: "
                                 f"{', '.join(cls.partials) or 'none'}")
    if (settings.partial is None) != (settings.full_every is None):
        raise HTTPException(400, "Set `partial` and `full_every` together, or neither")
    with storage.connect() as conn:
        storage.set_schedule(conn, source, settings.interval_minutes, settings.partial,
                             settings.full_every)
        return _schedule(conn, source)


@app.delete("/sources/{source}/schedule", dependencies=[Auth], tags=["status"],
            operation_id="resetSchedule", summary="Restore a source's default schedule",
            responses={**errors(401), **SOURCE_ERRORS})
def reset_schedule(source: str = SOURCE_PATH) -> Schedule:
    """Go back to the server's default: a full fetch every couple of hours (see the returned
    `interval_minutes`). Returns the default schedule."""
    _source(source)
    with storage.connect() as conn:
        storage.reset_schedule(conn, source)
        return _schedule(conn, source)


@app.put("/sources/{source}/browser", dependencies=[Auth], tags=["status"],
         operation_id="setBrowser", summary="Set whether a source runs in a headed browser",
         responses={**errors(401), **SOURCE_ERRORS})
def set_browser(settings: BrowserSettings, source: str = SOURCE_PATH) -> Browser:
    """Run `source` in a headed (visible) browser, or a headless one, from its next fetch on.
    A headed browser gets past bot checks (such as Cloudflare's on `vhl`) that stop a headless
    one; on a server it runs on a virtual display. Returns the new setting."""
    _source(source)
    with storage.connect() as conn:
        storage.set_browser(conn, source, settings.headed)
        return _browser(conn, source)


@app.delete("/sources/{source}/browser", dependencies=[Auth], tags=["status"],
            operation_id="resetBrowser", summary="Restore a source's default browser",
            responses={**errors(401), **SOURCE_ERRORS})
def reset_browser(source: str = SOURCE_PATH) -> Browser:
    """Go back to the source's own default (`vhl` is headed, the others headless). Returns the
    default setting."""
    _source(source)
    with storage.connect() as conn:
        storage.reset_browser(conn, source)
        return _browser(conn, source)


# The sync endpoints write, so they open their own read-write connection (which also
# creates the table on a database the current scraper hasn't touched yet).

@app.post(
    "/sync", dependencies=[Auth], status_code=202, tags=["status"], operation_id="requestSync",
    summary="Ask the scraper to refresh now",
    responses={200: {"model": SyncRequest, "description": "An equivalent request was already "
                                                          "waiting; that one is returned."},
               202: {"description": "Queued a new request."},
               **errors(400, 401)},
)
def request_sync(
    response: Response,
    source: list[str] | None = Query(
        None, description=f"Sources to sync: {SOURCE_NAMES}. Repeat for several. Omit to sync "
                          "every enabled source.", examples=["google_classroom"]),
) -> SyncRequest:
    """Queue a scrape. The scraper picks it up within seconds if it's idle, or after the run in
    progress. A scrape takes from under a minute (one small source) to about 10 minutes
    (everything), so poll `GET /sync/{request_id}` every 15–30 seconds until `status` is `done`
    or `failed`, then re-read the data. If a waiting request already covers these sources, that
    one is returned (200) instead of queueing another (202). Only call this when the user wants
    fresher data than `GET /sources` shows; the data refreshes on its own, on each source's
    `schedule`. A sync is always a full fetch, and the next scheduled fetch counts from it."""
    if source and (unknown := set(source) - REGISTRY.keys()):
        raise HTTPException(400, f"Unknown source(s): {', '.join(sorted(unknown))}. "
                                 f"Available: {', '.join(sorted(REGISTRY))}")
    with storage.connect() as conn:
        request_id, created = storage.request_sync(conn, source or None)
        row = conn.execute("SELECT * FROM sync_requests WHERE request_id=?", (request_id,)).fetchone()
    config.SYNC_TRIGGER.touch()
    if not created:
        response.status_code = 200
    return storage.sync_request_to_dict(row)


@app.get("/sync", dependencies=[Auth], tags=["status"], operation_id="listSyncRequests",
         summary="List recent sync requests", responses=errors(401))
def sync_requests(
    limit: int = Query(20, ge=1, le=200, description="How many to return."),
) -> list[SyncRequest]:
    """Recent manual sync requests, newest first."""
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_requests ORDER BY request_id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [storage.sync_request_to_dict(r) for r in rows]


@app.delete("/sync", dependencies=[Auth], tags=["status"], operation_id="clearSyncRequests",
            summary="Clear the sync request list", responses=errors(401))
def clear_sync_requests() -> ClearedSyncRequests:
    """Deletes finished requests and cancels waiting ones. Running ones stay, since their run
    still reports on them."""
    with storage.connect() as conn:
        return {"deleted": storage.clear_sync_requests(conn)}


@app.get("/sync/{request_id}", dependencies=[Auth], tags=["status"],
         operation_id="getSyncRequest", summary="Get one sync request",
         responses=errors(401, 404))
def sync_request(
    request_id: int = Path(description="`request_id` returned by `POST /sync`."),
) -> SyncRequest:
    """Poll this after `POST /sync` until `status` is `done` or `failed`."""
    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM sync_requests WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Sync request not found")
    return storage.sync_request_to_dict(row)


@app.get("/runs", dependencies=[Auth], tags=["status"], operation_id="listRuns",
         summary="List recent scrape runs", responses=READ_ERRORS)
def runs(
    source: str | None = _source_param(),
    failed: bool = Query(False, description="Only runs that failed."),
    limit: int = Query(20, ge=1, le=200, description="How many to return."),
    conn: sqlite3.Connection = Depends(db),
) -> list[Run]:
    """Scrape runs, newest first, without their logs (see `GET /runs/{run_id}`)."""
    sql, args = "SELECT * FROM scrape_runs WHERE 1=1", []
    if source:
        sql += " AND source=?"
        args.append(source)
    if failed:
        sql += " AND ok=0"
    sql += " ORDER BY run_id DESC LIMIT ?"
    args.append(limit)
    return [storage.run_to_dict(r) for r in conn.execute(sql, args)]


RUN_ID = Path(description="`run_id` from `GET /runs` or a source's `last_run`.")


def _run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM scrape_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Run not found")
    return row


@app.get("/runs/{run_id}", dependencies=[Auth], tags=["status"], operation_id="getRun",
         summary="Get one scrape run with its log", responses=errors(401, 404, 503))
def run(run_id: int = RUN_ID, conn: sqlite3.Connection = Depends(db)) -> RunDetail:
    """One scrape run, including `log` for a failed run."""
    return storage.run_to_dict(_run(conn, run_id), with_log=True)


@app.get("/runs/{run_id}/log", dependencies=[Auth], tags=["status"], operation_id="getRunLog",
         summary="Get a failed run's log as text", response_class=PlainTextResponse,
         responses={200: {"content": {"text/plain": {}}}, **errors(401, 404, 503)})
def run_log(run_id: int = RUN_ID, conn: sqlite3.Connection = Depends(db)) -> str:
    """A failed run's log as plain text, ready to paste: the error, the trail of log lines and
    browser activity leading up to it, and the traceback. 404 if the run kept no log (it
    succeeded, predates logs, or is older than the last 20 runs of its source)."""
    log = storage.run_to_dict(_run(conn, run_id), with_log=True)["log"]
    if log is None:
        raise HTTPException(404, "No log for this run (only recent failed runs keep one)")
    return log


@app.get("/db", dependencies=[Auth], tags=["status"], operation_id="downloadDatabase",
         summary="Download the whole database", response_class=FileResponse,
         responses={200: {"content": {"application/vnd.sqlite3": {
                        "schema": {"type": "string", "format": "binary"}}}},
                    **errors(401, 503)})
def database(conn: sqlite3.Connection = Depends(db)) -> FileResponse:
    """The whole SQLite database as one file (`lifeapi.db`), a consistent snapshot even while
    the scraper writes. It holds every record, run and setting, so another lifeapi install can
    use it as its `data/lifeapi.db`. Not useful to an agent: use the other endpoints instead."""
    fd, path = tempfile.mkstemp(prefix="lifeapi-", suffix=".db")
    os.close(fd)
    try:
        # The backup API copies page by page under a read transaction, so the snapshot is
        # consistent and includes whatever is still only in the -wal file.
        dest = sqlite3.connect(path)
        try:
            conn.backup(dest)
        finally:
            dest.close()
    except BaseException:
        os.unlink(path)
        raise
    return FileResponse(path, media_type="application/vnd.sqlite3", filename="lifeapi.db",
                        background=BackgroundTask(os.unlink, path))


@app.get("/courses", dependencies=[Auth], tags=["courses"], operation_id="listCourses",
         summary="List courses", responses=READ_ERRORS)
def courses(
    source: str | None = _source_param(),
    include_inactive: bool = INCLUDE_INACTIVE,
    conn: sqlite3.Connection = Depends(db),
) -> list[Course]:
    """Every course on every source, sorted by source then name. Use a course's `source` and
    `id` with `GET /items?source=…&course_id=…` to list its work."""
    sql, args = "SELECT * FROM courses WHERE 1=1", []
    if source:
        sql += " AND source=?"
        args.append(source)
    if not include_inactive:
        sql += " AND active=1"
    sql += " ORDER BY source, name"
    return [storage.row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/items", dependencies=[Auth], tags=["items"], operation_id="listItems",
         summary="Search and filter items", responses=READ_ERRORS)
def items(
    source: list[str] | None = _source_param(repeatable=True),
    kind: list[ItemKind] | None = Query(None, description="Only these kinds. Repeat to match "
                                                          "any of several."),
    course_id: str | None = Query(None, description="Only items in this course. Course ids "
                                                    "are per source, so pair with `source`."),
    status: list[str] | None = Query(
        None, description="Only these statuses, in the source's wording (e.g. `assigned`, "
                          "`missing`, `turned_in`, `graded`, `completed`). Repeat to match any "
                          "of several.", examples=["missing"]),
    due_after: datetime | None = Query(None, description="Due at or after this time (ISO 8601; "
                                                         "include an offset). Excludes items "
                                                         "with no due date."),
    due_before: datetime | None = Query(None, description="Due at or before this time (ISO "
                                                          "8601; include an offset). Excludes "
                                                          "items with no due date."),
    posted_after: datetime | None = Query(None, description="Posted at or after this time "
                                                            "(ISO 8601; include an offset)."),
    q: str | None = Query(None, description="Case-insensitive substring match on `title`, "
                                            "`course_name` or the student's note (not the "
                                            "description).",
                          examples=["essay"]),
    include_inactive: bool = INCLUDE_INACTIVE,
    order: Literal["due", "posted", "seen"] = Query(
        "due", description="`due`: soonest deadline first, undated last. `posted`: newest "
                           "first. `seen`: most recently discovered by the scraper first."),
    limit: int = Query(200, le=2000, description="Maximum items to return."),
    offset: int = Query(0, description="Items to skip, for paging."),
    conn: sqlite3.Connection = Depends(db),
) -> list[Item]:
    """Assignments, quizzes, questions, materials and announcements from every source, plus
    assignments made in lifeapi. Filters combine with AND; a repeated filter matches any of its
    values. Due-date filters and order use `due_at`, the student's deadline where they changed
    it. For "what's due" and "what's overdue", `GET /items/upcoming` and `GET /items/missing`
    are simpler."""
    sql, args = "SELECT * FROM item_view WHERE 1=1", []

    def any_of(col: str, values: list[str]) -> None:
        nonlocal sql
        sql += f" AND {col} IN ({','.join('?' * len(values))})"
        args.extend(values)

    if source:
        any_of("source", source)
    if kind:
        any_of("kind", [k.value for k in kind])
    if status:
        any_of("status", status)
    if course_id:
        sql += " AND course_id=?"
        args.append(course_id)
    if due_after:
        sql += " AND due_at>=?"
        args.append(_iso(due_after))
    if due_before:
        sql += " AND due_at<=?"
        args.append(_iso(due_before))
    if posted_after:
        sql += " AND posted_at>=?"
        args.append(_iso(posted_after))
    if q:
        sql += " AND (title LIKE ? OR course_name LIKE ? OR note LIKE ?)"
        args += [f"%{q}%"] * 3
    if not include_inactive:
        sql += " AND active=1"
    sql += {
        "due": " ORDER BY due_at IS NULL, due_at, posted_at DESC",
        "posted": " ORDER BY posted_at IS NULL, posted_at DESC",
        "seen": " ORDER BY first_seen_at DESC",
    }[order]
    sql += " LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [storage.item_to_dict(r) for r in conn.execute(sql, args)]


def _due_sql(start: datetime, end: datetime, include_done: bool = False,
             what: str = "*") -> tuple[str, list[Any]]:
    """Active items due from `start` to `end` (inclusive), without finished ones unless
    `include_done`. `/items/upcoming` and the `next` value share it."""
    sql = f"SELECT {what} FROM item_view WHERE active=1 AND due_at>=? AND due_at<=?"
    args: list[Any] = [_iso(start), _iso(end)]
    if not include_done:
        sql += f" AND (status IS NULL OR status NOT IN ({','.join('?' * len(DONE_STATUSES))}))"
        args += DONE_STATUSES
    return sql, args


def _missing_sql(what: str = "*", days: int | None = None) -> tuple[str, list[Any]]:
    """Overdue items not turned in, as `/items/missing` and the `missing` value count them;
    with `days`, only those due in the last `days` days."""
    now = datetime.now(timezone.utc)
    sql = f"""SELECT {what} FROM item_view WHERE active=1 AND due_at<? AND
              (status IN ('missing','assigned') OR status IS NULL) AND kind!='announcement'"""
    args: list[Any] = [_iso(now)]
    if days is not None:
        sql += " AND due_at>=?"
        args.append(_iso(now - timedelta(days=days)))
    return sql, args


@app.get("/items/upcoming", dependencies=[Auth], tags=["items"], operation_id="listUpcomingItems",
         summary="List work due soon", responses=READ_ERRORS)
def upcoming(
    days: int = Query(14, ge=1, le=365, description="How many days ahead to look."),
    include_done: bool = Query(False, description="Also include work already marked finished "
                                                  "(turned in, completed, graded…)."),
    conn: sqlite3.Connection = Depends(db),
) -> list[Item]:
    """Items due between now and `days` from now, soonest first. By default leaves out work
    already finished, so this is the to-do list. Deadlines the student changed count as
    changed, and assignments they made from announcements are included."""
    now = datetime.now(timezone.utc)
    sql, args = _due_sql(now, now + timedelta(days=days), include_done)
    return [storage.item_to_dict(r) for r in conn.execute(sql + " ORDER BY due_at", args)]


@app.get("/items/missing", dependencies=[Auth], tags=["items"], operation_id="listMissingItems",
         summary="List overdue work", responses=READ_ERRORS)
def missing(conn: sqlite3.Connection = Depends(db)) -> list[Item]:
    """Items past their deadline whose status is `missing`, `assigned` or empty (so not
    turned in on the source), most recently due first. Announcements are excluded. Some
    platforms keep old work listed long after it stops mattering, so expect a long tail. An
    item whose deadline the student moved later isn't overdue until the new one."""
    sql, args = _missing_sql()
    return [storage.item_to_dict(r) for r in conn.execute(sql + " ORDER BY due_at DESC", args)]


@app.get("/announcements", dependencies=[Auth], tags=["items"], operation_id="listAnnouncements",
         summary="List recent announcements", responses=READ_ERRORS)
def announcements(
    days: int = Query(14, ge=1, le=365, description="How many days back to look."),
    source: str | None = _source_param(),
    conn: sqlite3.Connection = Depends(db),
) -> list[Item]:
    """Items of kind `announcement` posted in the last `days` days, newest first. Only Google
    Classroom has announcements today."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    sql, args = "SELECT * FROM item_view WHERE active=1 AND kind='announcement' AND posted_at>=?", [_iso(since)]
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY posted_at DESC"
    return [storage.item_to_dict(r) for r in conn.execute(sql, args)]


ITEM_SOURCE = Path(description=f"The item's `source`: {SOURCE_NAMES}.")
ITEM_ID = Path(description="The item's `id`.")


def _item_row(conn: sqlite3.Connection, source: str, item_id: str) -> sqlite3.Row:
    row = storage.get_item(conn, source, item_id)
    if not row:
        raise HTTPException(404, "Item not found")
    return row


@app.get("/items/{source}/{item_id}", dependencies=[Auth], tags=["items"], operation_id="getItem",
         summary="Get one item", responses=errors(401, 404, 503))
def item(source: str = ITEM_SOURCE, item_id: str = ITEM_ID,
         conn: sqlite3.Connection = Depends(db)) -> Item:
    """One item by `(source, id)`, including inactive ones."""
    return storage.item_to_dict(_item_row(conn, source, item_id))


# Notes, deadlines and statuses write, so like /sync they open their own read-write connection. They're
# kept apart from the scraped record (in item_marks), so a scrape never overwrites them.

def _mark(source: str, item_id: str, **fields: str | None) -> dict[str, Any]:
    with storage.connect() as conn:
        _item_row(conn, source, item_id)
        storage.set_mark(conn, source, item_id, **fields)
        conn.commit()
        return storage.item_to_dict(storage.get_item(conn, source, item_id))


def _due_utc(value: datetime | date) -> str:
    """A deadline from a request as stored: UTC. Naive is school-local, a bare date 23:59."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, time(23, 59))
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return _iso(value)


@app.put("/items/{source}/{item_id}/note", dependencies=[Auth], tags=["items"],
         operation_id="setItemNote", summary="Set the student's note on an item",
         responses=errors(401, 404))
def set_note(settings: NoteSettings, source: str = ITEM_SOURCE, item_id: str = ITEM_ID) -> Item:
    """Replaces the item's note (`user.note`) with `note`; a blank one deletes it. Notes stay
    in lifeapi: the platform never sees them, and scrapes leave them alone. Returns the item."""
    return _mark(source, item_id, note=settings.note.strip() or None)


@app.delete("/items/{source}/{item_id}/note", dependencies=[Auth], tags=["items"],
            operation_id="deleteItemNote", summary="Delete the student's note on an item",
            responses=errors(401, 404))
def delete_note(source: str = ITEM_SOURCE, item_id: str = ITEM_ID) -> Item:
    """Returns the item."""
    return _mark(source, item_id, note=None)


@app.put("/items/{source}/{item_id}/due", dependencies=[Auth], tags=["items"],
         operation_id="setItemDue", summary="Change an item's deadline",
         responses=errors(401, 404))
def set_due(settings: DueSettings, source: str = ITEM_SOURCE, item_id: str = ITEM_ID) -> Item:
    """Sets the student's own deadline for the item (an extension, say, or an earlier personal
    one). From then on it's the item's `due_at` everywhere, in lists, filters and counts, and
    `source_due_at` keeps the platform's. If the platform's deadline changes later, `due_at`
    stays the student's until `DELETE` restores the platform's. Returns the item."""
    return _mark(source, item_id, due_at=_due_utc(settings.due_at))


@app.delete("/items/{source}/{item_id}/due", dependencies=[Auth], tags=["items"],
            operation_id="resetItemDue", summary="Restore an item's own deadline",
            responses=errors(401, 404))
def reset_due(source: str = ITEM_SOURCE, item_id: str = ITEM_ID) -> Item:
    """Drops the student's deadline, so `due_at` is the platform's again (`source_due_at`).
    An assignment made in lifeapi has no platform deadline, so it's then left without one.
    Returns the item."""
    return _mark(source, item_id, due_at=None)


@app.put("/items/{source}/{item_id}/status", dependencies=[Auth], tags=["items"],
         operation_id="setItemStatus", summary="Mark an item turned in or not",
         responses=errors(401, 404, 409))
def set_status(settings: StatusSettings, source: str = ITEM_SOURCE, item_id: str = ITEM_ID) -> Item:
    """Marks the item turned in (`{"turned_in": true}`) or not, in lifeapi only: the platform
    isn't told. On a scraped item, `status` becomes `turned_in` or `assigned` (`user.status`)
    while the platform's (`source_status`) says otherwise, so it leaves or joins
    `/items/upcoming` and `/items/missing`; once the platform agrees, its own wording (`graded`,
    say) shows again. Marking it the way the platform already has it drops the student's
    status. An assignment made in lifeapi becomes `done` or `assigned`. 409 for an
    announcement or material. Returns the item. The command `done` (`POST /extra/commands`)
    does the same, undoably."""
    with storage.connect() as conn:
        try:
            plan = commands.status_plan(storage.item_to_dict(_item_row(conn, source, item_id)),
                                        settings.turned_in)
        except commands.CommandError as e:
            raise HTTPException(e.status, e.message)
        storage.apply_fields(conn, source, item_id, plan.changes)
        conn.commit()
        return storage.item_to_dict(storage.get_item(conn, source, item_id))


@app.delete("/items/{source}/{item_id}/status", dependencies=[Auth], tags=["items"],
            operation_id="resetItemStatus", summary="Restore an item's own status",
            responses=errors(401, 404, 409))
def reset_status(source: str = ITEM_SOURCE, item_id: str = ITEM_ID) -> Item:
    """Drops the student's status, so `status` is the platform's again (`source_status`). 409
    for an assignment made in lifeapi, whose status is only ever the student's. Returns the
    item."""
    with storage.connect() as conn:
        if _item_row(conn, source, item_id)["converted_from"] is not None:
            raise HTTPException(409, "It was made in lifeapi, so it has no platform status to go "
                                     "back to; mark it turned in or not instead.")
    return _mark(source, item_id, status=None)


@app.get("/grades", dependencies=[Auth], tags=["grades"], operation_id="listGrades",
         summary="List course grades", responses=READ_ERRORS)
def grades(
    source: str | None = _source_param(),
    term: str | None = Query(None, description="Only this term, exactly as it appears in "
                                               "`term` (e.g. `MP1`).", examples=["MP1"]),
    include_inactive: bool = INCLUDE_INACTIVE,
    conn: sqlite3.Connection = Depends(db),
) -> list[Grade]:
    """Course grades, one per course × term × grading task, sorted by course then term. A course
    usually has a `MARKING PERIOD` grade per term plus a running `FINAL AVERAGE`; `entries`
    lists the scored assignments behind each one. Overall GPAs (e.g. `Cumulative GPA`) are also
    listed here, with `gpa` set and no `course_id`."""
    sql, args = "SELECT * FROM grades WHERE 1=1", []
    if source:
        sql += " AND source=?"
        args.append(source)
    if term:
        sql += " AND term=?"
        args.append(term)
    if not include_inactive:
        sql += " AND active=1"
    sql += " ORDER BY course_name, term"
    return [storage.row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/history", dependencies=[Auth], tags=["history"], operation_id="listHistory",
         summary="List changes to grades, scores and statuses", responses=READ_ERRORS)
def history(
    source: str | None = _source_param(),
    kind: list[Literal["grade", "entry", "item"]] | None = Query(
        None, description="Only these kinds. Repeat to match any of several."),
    id: str | None = Query(None, description="Only this grade or item (its `id`). A grade's id "
                                             "also matches its entries.",
                           examples=["781223:6114:1"]),
    gpa: bool = Query(False, description="Only overall GPA records."),
    since: datetime | None = Query(None, description="Recorded at or after this time (ISO 8601; "
                                                     "include an offset)."),
    limit: int = Query(500, ge=1, le=5000, description="Maximum rows to return."),
    conn: sqlite3.Connection = Depends(db),
) -> list[HistoryRecord]:
    """Every recorded change to a grade-related value, newest first. A row is added when the
    scraper sees a value differ from the last one recorded (including the first time it sees
    a record), so the rows for one record are its full history, and each value holds until
    the next row. Kept for good, unlike `GET /runs`. Recording began when this endpoint was
    added, so older changes aren't here."""
    sql, args = "SELECT * FROM history WHERE 1=1", []
    if source:
        sql += " AND source=?"
        args.append(source)
    if kind:
        sql += f" AND kind IN ({','.join('?' * len(kind))})"
        args += kind
    if id is not None:
        sql += " AND id=?"
        args.append(id)
    if gpa:
        sql += " AND kind='grade' AND json_extract(value, '$.gpa') IS NOT NULL"
    if since:
        sql += " AND recorded_at>=?"
        args.append(_iso(since))
    sql += " ORDER BY history_id DESC LIMIT ?"
    args.append(limit)
    return [{**dict(r), "value": json.loads(r["value"])} for r in conn.execute(sql, args)]


# Single values for widgets and displays: each is served at /min/<name> as a bare number in
# text/plain and at /json/<name> as a small JSON object. Neither needs a token. Bodies are
# written by hand so the GPA keeps its trailing zeros (99.150 would serialize as 99.15).

MIN_RESPONSES = {200: {"content": {"text/plain": {"schema": {"type": "string"}}}}}


def _value(name: str, summary: str, doc: str, model: type, responses: dict | None = None):
    """Register `fn(conn, **query params) -> (number as text, extra JSON fields)` as GET
    /min/<name> and GET /json/<name>. Both take `fn`'s parameters (its `conn` defaults to
    `Depends(db)`, the rest to `Query(...)`), so FastAPI parses them the same way."""
    op = name.title()
    responses = {**(responses or {}), **errors(503)}
    min_doc = f"{doc}\n\nThe number alone, as plain text. `GET /json/{name}` has it as JSON."
    json_doc = f"{doc}\n\nAs JSON. `GET /min/{name}` has the number alone, as plain text."

    def register(fn):
        def as_text(**kwargs: Any) -> Response:
            text, _ = fn(**kwargs)
            return PlainTextResponse(text)

        def as_json(**kwargs: Any) -> Response:
            text, fields = fn(**kwargs)
            rest = "".join(f", {json.dumps(k)}: {json.dumps(v)}" for k, v in fields.items())
            return Response(f'{{"value": {text}{rest}}}', media_type="application/json")

        for route in (as_text, as_json):
            route.__signature__ = inspect.signature(fn)  # what FastAPI reads parameters from
        app.get(f"/min/{name}", tags=["values"], operation_id=f"get{op}Text", summary=summary,
                description=min_doc, response_class=PlainTextResponse,
                responses={**MIN_RESPONSES, **responses})(as_text)
        app.get(f"/json/{name}", tags=["values"], operation_id=f"get{op}", summary=summary,
                description=json_doc, response_model=model, responses=responses)(as_json)
        return fn

    return register


def _count(conn: sqlite3.Connection, query: tuple[str, list[Any]]) -> tuple[str, dict]:
    sql, args = query
    return str(conn.execute(sql, args).fetchone()[0]), {}


@_value("gpa", "Overall GPA, as a percentage", model=GpaValue,
        responses={404: {"model": Error, "description": "No GPA has been scraped yet."}},
        doc="""The overall GPA, always written with three decimal places, e.g. `99.150`. A JSON
parser reads that as `99.15`, so pad it to three places again to show it. This school's GPA is
already a percentage (as Infinite Campus shows it, to three places), so the value is the GPA as
published. It's weighted, so honors and AP courses can lift it above 100. When several GPA
records exist, this is the cumulative weighted one. 404 until a GPA has been scraped. Every GPA
record (term, unweighted, rank) is in `GET /grades`, with `gpa` set, behind the token.""")
def _gpa(conn: sqlite3.Connection = Depends(db)) -> tuple[str, dict]:
    rows = conn.execute(
        "SELECT * FROM grades WHERE active=1 AND json_extract(data, '$.gpa') IS NOT NULL"
    )
    records = [storage.row_to_dict(r) for r in rows]
    if not records:
        raise HTTPException(404, "No GPA yet")
    best = max(records, key=lambda g: (g["extra"].get("type") == "cumulative",
                                       g["extra"].get("weighted", True),
                                       g["extra"].get("calendar_id") or 0))
    return f"{best['gpa']:.{GPA_PLACES}f}", {"last_seen_at": best["last_seen_at"]}


@_value("missing", "Number of recently overdue items", model=CountValue,
        doc="How many items are past their deadline and not turned in (as in `GET "
            "/items/missing`), counting only those due in the last `days` days.")
def _missing_count(
    days: int = Query(7, ge=1, le=3650, description="How many days back to count."),
    conn: sqlite3.Connection = Depends(db),
) -> tuple[str, dict]:
    return _count(conn, _missing_sql("COUNT(*)", days))


@_value("next", "Number of items due soon", model=CountValue,
        doc="How many items `GET /items/upcoming` lists for the same `days`: unfinished and "
            "due between now and `days` days from now.")
def _next_count(
    days: int = Query(7, ge=1, le=365, description="How many days ahead to count."),
    conn: sqlite3.Connection = Depends(db),
) -> tuple[str, dict]:
    now = datetime.now(timezone.utc)
    return _count(conn, _due_sql(now, now + timedelta(days=days), what="COUNT(*)"))


@_value("status", "Number of failing sources", model=StatusValue,
        doc="How many enabled sources' most recent run failed, so 0 means every source is "
            "fine. `GET /sources` (behind the token) says which, and why. The JSON adds "
            "`minutes`: how stale the stalest source is.")
def _status(conn: sqlite3.Connection = Depends(db)) -> tuple[str, dict]:
    enabled = [name for name, cls in REGISTRY.items() if cls.enabled]
    marks = ",".join("?" * len(enabled))
    failing = conn.execute(
        f"""SELECT COUNT(*) FROM scrape_runs r
            JOIN (SELECT source, MAX(run_id) AS run_id FROM scrape_runs GROUP BY source) last
              USING (source, run_id)
            WHERE r.ok=0 AND r.source IN ({marks})""", enabled,
    ).fetchone()[0]
    # Each enabled source's last successful full run (`last_success_at` in /sources).
    updated = dict(conn.execute(
        f"""SELECT source, MAX(finished_at) FROM scrape_runs
            WHERE ok=1 AND partial IS NULL AND source IN ({marks}) GROUP BY source""", enabled,
    ).fetchall())
    minutes = None
    if enabled and all(updated.get(name) for name in enabled):
        oldest = datetime.fromisoformat(min(updated[name] for name in enabled))
        minutes = int((datetime.now(timezone.utc) - oldest).total_seconds() // 60)
    return str(failing), {"minutes": minutes}


# Turning announcements into assignments. Assignments made here are items like any other
# (source and course of their announcement, id `lifeapi-<n>`), stored in custom_items; their
# deadline and note are item_marks, set like any item's.

EXTRA_ERRORS = errors(401, 404, 409)


def _announcement(conn: sqlite3.Connection, source: str, item_id: str) -> sqlite3.Row:
    row = _item_row(conn, source, item_id)
    if row["kind"] != ItemKind.ANNOUNCEMENT.value:
        raise HTTPException(409, f"That's {'an' if row['kind'][0] in 'aeiou' else 'a'} "
                                 f"{row['kind']}; only announcements convert to assignments")
    return row


def _custom(conn: sqlite3.Connection, source: str, item_id: str) -> sqlite3.Row:
    row = _item_row(conn, source, item_id)
    if row["converted_from"] is None:
        raise HTTPException(409, "That item comes from its platform, not lifeapi, so it can't be "
                                 "edited here (its note, deadline and status can: "
                                 "/items/{source}/{item_id}/note, /due, /status)")
    return row


@app.get("/extra/drafts/{source}/{item_id}", dependencies=[Auth], tags=["extra"],
         operation_id="draftAssignment", summary="Suggest assignments from an announcement",
         responses={**EXTRA_ERRORS, **errors(503)})
def draft_assignment(source: str = ITEM_SOURCE,
                     item_id: str = Path(description="The announcement's `id`."),
                     conn: sqlite3.Connection = Depends(db)) -> ConversionDraft:
    """Reads the announcement and suggests the assignments it asks for: a title, kind, deadline
    and description for each, every date it mentions, and existing items it may be about. Fill
    a form with them for the student to check, then `POST /extra/assignments`. Dates count from
    when the announcement was posted, and a date written without a time gets the time the
    course's deadlines are usually set at. Nothing is saved."""
    ann = storage.item_to_dict(_announcement(conn, source, item_id))
    course = [storage.item_to_dict(r) for r in conn.execute(
        "SELECT * FROM item_view WHERE source=? AND course_id IS ? AND active=1 AND id!=?",
        (source, ann["course_id"], item_id))]
    converted = [i["id"] for i in course if i["converted_from"] == item_id]
    out = drafts.draft(ann, [i for i in course if i["converted_from"] != item_id])
    return {"source": source, "id": item_id, **out, "converted": converted}


@app.post("/extra/assignments", dependencies=[Auth], tags=["extra"], status_code=201,
          operation_id="createAssignment", summary="Make an assignment from an announcement",
          responses=EXTRA_ERRORS)
def create_assignment(body: NewAssignment) -> Item:
    """Makes an assignment from an announcement. It joins the announcement's course and source
    with an id starting `lifeapi-`, links to the announcement (`url`, `converted_from`), keeps its
    attachments, and from then on appears in `/items`, `/items/upcoming` and `/items/missing`
    like scraped work. Its `status` starts `assigned`; set it to `done` with `PATCH
    /extra/assignments/{source}/{item_id}`. Returns the new item."""
    with storage.connect() as conn:
        ann = storage.item_to_dict(_announcement(conn, body.source, body.announcement_id))
        record = ItemRecord(
            source=body.source, id="", kind=body.kind, title=body.title.strip(), url=ann["url"],
            course_id=ann["course_id"], course_name=ann["course_name"],
            description=(body.description or "").strip() or None, author=ann["author"],
            posted_at=ann["posted_at"], status="assigned", points_possible=body.points_possible,
            attachments=ann["attachments"],
        )
        new_id = storage.create_custom_item(conn, record, body.announcement_id)
        note = (body.note or "").strip() or None
        if body.due_at is not None or note:
            storage.set_mark(conn, body.source, new_id, note=note,
                             due_at=None if body.due_at is None else _due_utc(body.due_at))
        conn.commit()
        return storage.item_to_dict(storage.get_item(conn, body.source, new_id))


@app.get("/extra/assignments", dependencies=[Auth], tags=["extra"],
         operation_id="listAssignments", summary="List assignments made from announcements",
         responses=READ_ERRORS)
def assignments(
    source: str | None = _source_param(),
    announcement_id: str | None = Query(None, description="Only those made from this "
                                                          "announcement (pair with `source`)."),
    conn: sqlite3.Connection = Depends(db),
) -> list[Item]:
    """Every assignment made in lifeapi, newest first. They're in `GET /items` too; this lists
    just them."""
    sql, args = "SELECT * FROM item_view WHERE converted_from IS NOT NULL", []
    if source:
        sql += " AND source=?"
        args.append(source)
    if announcement_id:
        sql += " AND converted_from=?"
        args.append(announcement_id)
    return [storage.item_to_dict(r) for r in conn.execute(sql + " ORDER BY first_seen_at DESC", args)]


@app.patch("/extra/assignments/{source}/{item_id}", dependencies=[Auth], tags=["extra"],
           operation_id="editAssignment", summary="Edit or finish an assignment made from an announcement",
           responses=EXTRA_ERRORS)
def edit_assignment(edit: AssignmentEdit, source: str = ITEM_SOURCE,
                    item_id: str = ITEM_ID) -> Item:
    """Changes the fields given, e.g. `{"status": "done"}` once it's handed in. Returns the
    item."""
    with storage.connect() as conn:
        record = ItemRecord.model_validate_json(_custom(conn, source, item_id)["data"])
        for name in edit.model_fields_set:
            value = getattr(edit, name)
            if name in ("title", "kind", "status") and value is None:
                continue  # these can't be cleared
            if isinstance(value, str):
                value = value.strip() or None
            setattr(record, name, ItemKind(value) if name == "kind" else value)
        storage.save_custom_item(conn, record)
        conn.commit()
        return storage.item_to_dict(storage.get_item(conn, source, item_id))


@app.delete("/extra/assignments/{source}/{item_id}", dependencies=[Auth], tags=["extra"],
            status_code=204, operation_id="deleteAssignment",
            summary="Delete an assignment made from an announcement", responses=EXTRA_ERRORS)
def delete_assignment(source: str = ITEM_SOURCE, item_id: str = ITEM_ID) -> Response:
    """Deletes it for good, with its note and deadline. The announcement stays."""
    with storage.connect() as conn:
        _custom(conn, source, item_id)
        storage.delete_custom_item(conn, source, item_id)
        conn.commit()
    return Response(status_code=204)


# Commands in words. Each applied command is an `actions` row holding the values it replaced,
# so any of them can be undone later; an undo is an action too, so it can be undone (redo).

def _changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    shown = lambda name, value: storage.local_iso(value) if name == "due_at" else value
    return [{"field": k, "before": shown(k, before.get(k)), "after": shown(k, v)} for k, v in after.items()]


def _unsaved(item: dict[str, Any], command: str, summary: str, notes: list[str],
             changes: list[dict[str, Any]], undo_of: int | None = None,
             kind: str = "command") -> dict[str, Any]:
    """A command result that changed nothing: a dry run, or the item already was that way."""
    return {"action_id": None, "kind": kind, "source": item["source"], "item_id": item["id"],
            "title": item["title"], "command": command, "summary": summary, "notes": notes,
            "changes": changes, "undo_of": undo_of, "undone_by": None, "created_at": None,
            "item": item}


# An action with its kind: an undo of an undo is a redo.
ACTIONS_SQL = """SELECT a.*, CASE WHEN a.undo_of IS NULL THEN 'command'
                    WHEN (SELECT b.undo_of FROM actions b WHERE b.action_id = a.undo_of) IS NULL THEN 'undo'
                    ELSE 'redo' END AS kind FROM actions a"""


def _action(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "action_id": row["action_id"], "kind": row["kind"], "source": row["source"], "item_id": row["id"],
        "title": row["title"], "command": row["command"], "summary": row["summary"],
        "notes": json.loads(row["notes"]),
        "changes": _changes(json.loads(row["before"]), json.loads(row["after"])),
        "undo_of": row["undo_of"], "undone_by": row["undone_by"], "created_at": row["created_at"],
    }


def _action_row(conn: sqlite3.Connection, action_id: int) -> sqlite3.Row:
    row = conn.execute(f"{ACTIONS_SQL} WHERE a.action_id=?", (action_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No command with that id")
    return row


def _command_result(conn: sqlite3.Connection, action_id: int) -> dict[str, Any]:
    out = _action(_action_row(conn, action_id))
    row = storage.get_item(conn, out["source"], out["item_id"])
    return {**out, "item": row and storage.item_to_dict(row)}


def _undo(conn: sqlite3.Connection, target: sqlite3.Row, command: str,
          dry_run: bool) -> dict[str, Any]:
    """Undo `target`: put back the values it replaced, logged as a new action."""
    if target["undone_by"]:
        raise HTTPException(409, f"That was already undone (command {target['undone_by']}).")
    source, item_id = target["source"], target["id"]
    item = storage.item_to_dict(_item_row(conn, source, item_id))
    restore, expected = json.loads(target["before"]), json.loads(target["after"])
    redo = target["undo_of"] is not None
    said = _action_row(conn, target["undo_of"])["command"] if redo else target["command"]
    if redo and command == "undo":  # the undo endpoint on an undo
        command = "redo"
    verb = "Redid" if redo else "Undid"
    current = storage.item_fields(conn, source, item_id, restore)
    if dry_run:
        return _unsaved(item, command, f"Would {'redo' if redo else 'undo'} {commands.quote(said)}.",
                        [], _changes(current, restore), undo_of=target["action_id"],
                        kind="redo" if redo else "undo")
    storage.apply_fields(conn, source, item_id, restore)
    after = storage.item_to_dict(storage.get_item(conn, source, item_id))
    now = datetime.now(LOCAL_TZ)
    parts = []
    if "due_at" in restore:
        due = commands.when_text(commands.local(after["due_at"]), now)
        parts.append(f"due date back to {commands.source_name(source)}'s, {due}"
                     if restore["due_at"] is None and after["converted_from"] is None else f"due {due}")
    if "note" in restore:
        parts.append(f"note back to {commands.quote(restore['note'])}" if restore["note"] else "note removed")
    if "status" in restore:
        status = restore["status"]
        parts.append(f"status back to {commands.source_name(source)}'s, {commands.label(after['status'])}"
                     if status is None else "marked done" if status == "done"
                     else "marked turned in" if status == "turned_in"
                     else "marked not turned in" if after["converted_from"] is None else "marked not done")
    notes = ["It had been changed since, so that later change is replaced too."] if current != expected else []
    action_id = storage.record_action(
        conn, source, item_id, item["title"], command,
        f"{verb} {commands.quote(said)}: {', '.join(parts)}.", notes, current, restore,
        undo_of=target["action_id"])
    conn.commit()
    return _command_result(conn, action_id)


@app.post("/extra/commands", dependencies=[Auth], tags=["extra"], operation_id="runCommand",
          summary="Change an item with a command in words",
          responses={**errors(401, 404, 409), 400: {"model": Error, "description":
                     "The command wasn't understood (`detail` says what can be said), or no item "
                     "was named."}})
def run_command(body: CommandRequest) -> CommandResult:
    """Runs a command such as `set the due date to today at 11:59 PM`, `due at 11:59`, `due oct
    8 3 o'clock`, `push it back a day`, `note: bring a calculator` or `undo` on one item, and
    says what it did. It's forgiving: what isn't said is kept from the item (a time alone keeps
    the date, a date alone the time), a time without am/pm is read as a student would mean it,
    and typos and spoken numbers are fine. `summary` and `notes` are written to show the person,
    including how anything ambiguous was read. Every command can be undone, however old: `undo`
    as a command undoes this item's latest, and `POST /extra/commands/{action_id}/undo` any one.
    `done` (`turned in`, `submitted`…) and `not done` (`assigned`, `unsubmit`…) mark it in
    lifeapi only, as `PUT /items/{source}/{item_id}/status` does. A command that changes nothing
    (it's already so) returns `action_id: null`. 400 when it isn't understood, with what can be
    said; 409 when it can't apply (moving a deadline it doesn't have, marking an announcement or
    material done)."""
    command, url = body.command, body.url
    if url and "##" in url:
        url, _, spoken = url.partition("##")
        command = command or spoken
    if not command or not command.strip():
        raise HTTPException(400, f"Say what to do, like {commands.EXAMPLES}.")
    with storage.connect() as conn:
        if url:
            found = storage.find_item_by_url(conn, url)
            if not found:
                raise HTTPException(404, "No item has that link. It may not have been scraped yet.")
            source, item_id = found
        elif body.source and body.item_id:
            source, item_id = body.source, body.item_id
        else:
            raise HTTPException(400, "Name the item: `source` and `item_id`, or `url`")
        item = storage.item_to_dict(_item_row(conn, source, item_id))
        usual = None
        if item["due_at"] is None:  # a date said without a time then gets the course's usual time
            usual = drafts.usual_time([storage.item_to_dict(r) for r in conn.execute(
                "SELECT * FROM item_view WHERE source=? AND course_id IS ? AND active=1",
                (source, item["course_id"]))])
        now = datetime.now(LOCAL_TZ)
        try:
            plan = commands.interpret(command, item, now, usual)
        except commands.CommandError as e:
            raise HTTPException(e.status, e.message)
        if plan in (commands.UNDO, commands.REDO):
            undo = plan == commands.UNDO
            target = conn.execute(
                f"{ACTIONS_SQL} WHERE a.source=? AND a.id=? AND a.undone_by IS NULL AND a.undo_of IS "
                f"{'NULL' if undo else 'NOT NULL'} ORDER BY a.action_id DESC LIMIT 1", (source, item_id)
            ).fetchone()
            if not target:
                raise HTTPException(409, f"There's nothing to {'undo' if undo else 'redo'} on this item.")
            return _undo(conn, target, command.strip(), body.dry_run)
        before = storage.item_fields(conn, source, item_id, plan.changes)
        if not plan.changes or body.dry_run:
            summary = f"Would do this: {plan.summary}" if plan.changes else plan.summary
            return _unsaved(item, command.strip(), summary, plan.notes, _changes(before, plan.changes))
        storage.apply_fields(conn, source, item_id, plan.changes)
        action_id = storage.record_action(conn, source, item_id, item["title"], command.strip(),
                                          plan.summary, plan.notes, before, plan.changes)
        conn.commit()
        return _command_result(conn, action_id)


@app.get("/extra/commands", dependencies=[Auth], tags=["extra"], operation_id="listCommands",
         summary="List commands that ran", responses=READ_ERRORS)
def list_commands(
    source: str | None = _source_param(),
    item_id: str | None = Query(None, description="Only those on this item (pair with `source`)."),
    limit: int = Query(50, ge=1, le=500, description="How many to return."),
    conn: sqlite3.Connection = Depends(db),
) -> list[Action]:
    """Commands that changed something, newest first, undos included. Any without `undone_by`
    can still be undone."""
    sql, args = f"{ACTIONS_SQL} WHERE 1=1", []
    if source:
        sql += " AND a.source=?"
        args.append(source)
    if item_id:
        sql += " AND a.id=?"
        args.append(item_id)
    sql += " ORDER BY a.action_id DESC LIMIT ?"
    args.append(limit)
    return [_action(r) for r in conn.execute(sql, args)]


COMMAND_ID = Path(description="`action_id` of a command.")


@app.get("/extra/commands/{action_id}", dependencies=[Auth], tags=["extra"],
         operation_id="getCommand", summary="Get one command and its item now",
         responses=errors(401, 404, 503))
def get_command(action_id: int = COMMAND_ID, conn: sqlite3.Connection = Depends(db)) -> CommandResult:
    return _command_result(conn, action_id)


@app.post("/extra/commands/{action_id}/undo", dependencies=[Auth], tags=["extra"],
          operation_id="undoCommand", summary="Undo a command", responses=errors(401, 404, 409))
def undo_command(action_id: int = COMMAND_ID,
                 dry_run: bool = Query(False, description="Say what it would do without doing it.")
                 ) -> CommandResult:
    """Puts back what the command replaced, however long ago it ran, and returns the undo (an
    action itself, so undoing it redoes the command). If the same fields changed since, those
    later changes are replaced too, and `notes` says so. 409 if it was already undone."""
    with storage.connect() as conn:
        return _undo(conn, _action_row(conn, action_id), "undo", dry_run)
