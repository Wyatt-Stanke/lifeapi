"""HTTP API over the scraped data. Runs independently of the scraper and only reads the
SQLite database the scraper writes, except for queueing and clearing manual sync requests
(`/sync`) and setting fetch schedules (`/sources/{source}/schedule`) and browser settings
(`/sources/{source}/browser`), which the scraper picks up.

The OpenAPI spec is meant to stand on its own (handed to a person or an agent without the
code), so keep summaries, descriptions and `schemas.py` in step with behavior."""

from __future__ import annotations

import shlex
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import config, storage
from ..models import GPA_PLACES, ItemKind
from ..scraper import sources as _sources  # noqa: F401  (registers all sources)
from ..scraper.base import REGISTRY, Source
from .schemas import (
    DESCRIPTION, DONE_STATUSES, TAGS, Browser, BrowserSettings, ClearedSyncRequests, Course,
    Error, Grade, Health, Item, Run, RunDetail, Schedule, ScheduleSettings, SourceStatus,
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


def require_token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if config.API_TOKEN and (creds is None or creds.credentials != config.API_TOKEN):
        raise HTTPException(401, "Missing or invalid bearer token")


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


def _login_command(source: str, error: str | None) -> str | None:
    """The command that finishes a stuck sign-in by hand (deploy/reauth.py), for a run that
    failed on a login challenge. Run it from a checkout of this repository."""
    if not error or not error.startswith("LoginError:"):
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
        out.append({
            "source": name,
            "enabled": name in REGISTRY and REGISTRY[name].enabled,
            "partials": [{"name": k, "description": v} for k, v in partials.items()],
            "schedule": _schedule(conn, name),
            "browser": _browser(conn, name),
            "last_run": r and storage.run_to_dict(r),
            "last_success_at": ok["finished_at"] if ok else None,
            "login_command": _login_command(name, r and r["error"]),
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
    q: str | None = Query(None, description="Case-insensitive substring match on `title` or "
                                            "`course_name` (not the description).",
                          examples=["essay"]),
    include_inactive: bool = INCLUDE_INACTIVE,
    order: Literal["due", "posted", "seen"] = Query(
        "due", description="`due`: soonest deadline first, undated last. `posted`: newest "
                           "first. `seen`: most recently discovered by the scraper first."),
    limit: int = Query(200, le=2000, description="Maximum items to return."),
    offset: int = Query(0, description="Items to skip, for paging."),
    conn: sqlite3.Connection = Depends(db),
) -> list[Item]:
    """Assignments, quizzes, questions, materials and announcements from every source. Filters
    combine with AND; a repeated filter matches any of its values. For "what's due" and
    "what's overdue", `GET /items/upcoming` and `GET /items/missing` are simpler."""
    sql, args = "SELECT * FROM items WHERE 1=1", []

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
        sql += " AND (title LIKE ? OR course_name LIKE ?)"
        args += [f"%{q}%"] * 2
    if not include_inactive:
        sql += " AND active=1"
    sql += {
        "due": " ORDER BY due_at IS NULL, due_at, posted_at DESC",
        "posted": " ORDER BY posted_at IS NULL, posted_at DESC",
        "seen": " ORDER BY first_seen_at DESC",
    }[order]
    sql += " LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [storage.row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/items/upcoming", dependencies=[Auth], tags=["items"], operation_id="listUpcomingItems",
         summary="List work due soon", responses=READ_ERRORS)
def upcoming(
    days: int = Query(14, ge=1, le=365, description="How many days ahead to look."),
    include_done: bool = Query(False, description="Also include work already marked finished "
                                                  "(turned in, completed, graded…)."),
    conn: sqlite3.Connection = Depends(db),
) -> list[Item]:
    """Items due between now and `days` from now, soonest first. By default leaves out work
    already finished, so this is the to-do list."""
    now = datetime.now(timezone.utc)
    sql = "SELECT * FROM items WHERE active=1 AND due_at>=? AND due_at<=?"
    if not include_done:
        sql += f" AND (status IS NULL OR status NOT IN ({','.join('?' * len(DONE_STATUSES))}))"
    sql += " ORDER BY due_at"
    args = [_iso(now), _iso(now + timedelta(days=days))] + ([] if include_done else list(DONE_STATUSES))
    return [storage.row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/items/missing", dependencies=[Auth], tags=["items"], operation_id="listMissingItems",
         summary="List overdue work", responses=READ_ERRORS)
def missing(conn: sqlite3.Connection = Depends(db)) -> list[Item]:
    """Items past their deadline whose status is `missing`, `assigned` or empty (so not
    turned in on the source), most recently due first. Announcements are excluded. Some
    platforms keep old work listed long after it stops mattering, so expect a long tail."""
    now = datetime.now(timezone.utc)
    sql = """SELECT * FROM items WHERE active=1 AND due_at<? AND
             (status IN ('missing','assigned') OR status IS NULL) AND kind!='announcement'
             ORDER BY due_at DESC"""
    return [storage.row_to_dict(r) for r in conn.execute(sql, (_iso(now),))]


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
    sql, args = "SELECT * FROM items WHERE active=1 AND kind='announcement' AND posted_at>=?", [_iso(since)]
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY posted_at DESC"
    return [storage.row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/items/{source}/{item_id}", dependencies=[Auth], tags=["items"], operation_id="getItem",
         summary="Get one item", responses=errors(401, 404, 503))
def item(
    source: str = Path(description=f"The item's `source`: {SOURCE_NAMES}."),
    item_id: str = Path(description="The item's `id`."),
    conn: sqlite3.Connection = Depends(db),
) -> Item:
    """One item by `(source, id)`, including inactive ones."""
    row = conn.execute("SELECT * FROM items WHERE source=? AND id=?", (source, item_id)).fetchone()
    if not row:
        raise HTTPException(404, "Item not found")
    return storage.row_to_dict(row)


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


@app.get("/gpa", tags=["grades"], operation_id="getGpa", response_model=float,
         summary="Get the overall GPA as a percentage",
         responses={200: {"headers": {"X-Last-Seen-At": {
                        "description": "When the scraper last read this GPA from the source (the "
                                       "record's `last_seen_at`), as a UTC ISO 8601 time.",
                        "schema": {"type": "string", "format": "date-time"}}}},
                    404: {"model": Error, "description": "No GPA has been scraped yet."},
                    **errors(503)})
def gpa(conn: sqlite3.Connection = Depends(db)) -> Response:
    """The overall GPA as a bare JSON number, always written with three decimal places, e.g.
    `99.150`. A JSON parser reads that as `99.15`, so pad it to three places again to show
    it. This school's GPA is already a percentage (as Infinite Campus shows it, to three
    places), so the value is the GPA as published. It's weighted, so honors and AP courses
    can lift it above 100. When several GPA records exist, this is the cumulative weighted
    one. The `X-Last-Seen-At` header says when it was last read from the source. 404 until a
    GPA has been scraped. Needs no token, so a display like the explorer's `/biggpa` page
    works on any device. Every GPA record (term, unweighted, rank) is in `GET /grades`, with
    `gpa` set, behind the token."""
    rows = conn.execute(
        "SELECT * FROM grades WHERE active=1 AND json_extract(data, '$.gpa') IS NOT NULL"
    )
    records = [storage.row_to_dict(r) for r in rows]
    if not records:
        raise HTTPException(404, "No GPA yet")
    best = max(records, key=lambda g: (g["extra"].get("type") == "cumulative",
                                       g["extra"].get("weighted", True),
                                       g["extra"].get("calendar_id") or 0))
    # Written by hand: serializing the float would drop trailing zeros (99.150 -> 99.15).
    return Response(f"{best['gpa']:.{GPA_PLACES}f}", media_type="application/json",
                    headers={"X-Last-Seen-At": best["last_seen_at"]})
