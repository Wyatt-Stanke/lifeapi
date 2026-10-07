"""HTTP API over the scraped data. Runs independently of the scraper and only reads the
SQLite database the scraper writes, except for queueing manual sync requests (`/sync`),
which the scraper picks up.

The OpenAPI spec is meant to stand on its own (handed to a person or an agent without the
code), so keep summaries, descriptions and `schemas.py` in step with behavior."""

from __future__ import annotations

import shlex
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import config, storage
from ..models import ItemKind
from ..scraper import sources as _sources  # noqa: F401  (registers all sources)
from ..scraper.base import REGISTRY
from .schemas import (
    DESCRIPTION, DONE_STATUSES, TAGS, Course, Grade, Health, Item, SourceStatus, SyncRequest,
    errors,
)

app = FastAPI(
    title="lifeapi",
    summary="Schoolwork from every platform, in one place.",
    description=DESCRIPTION,
    version="1.0.0",
    openapi_tags=TAGS,
)

_bearer = HTTPBearer(
    auto_error=False,
    description="Required only when the server sets `LIFEAPI_API_TOKEN`.",
)


def require_token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if config.API_TOKEN and (creds is None or creds.credentials != config.API_TOKEN):
        raise HTTPException(401, "Missing or invalid bearer token")


def db() -> Iterator[sqlite3.Connection]:
    if not config.DB_PATH.exists():
        raise HTTPException(503, "No data yet; run the scraper first")
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


@app.get("/sources", dependencies=[Auth], tags=["status"], operation_id="listSources",
         summary="List sources and their last scrape", responses=READ_ERRORS)
def sources(conn: sqlite3.Connection = Depends(db)) -> list[SourceStatus]:
    """Every source with its most recent scrape run (null if it has never run) and when it
    last succeeded. Check `last_success_at` to see how fresh a source's data is; a failed run
    keeps the previous data, so a source can be stale even though it returns records.
    `login_command` is set when the last run got stuck on a sign-in challenge that a human
    has to finish."""
    rows = {r["source"]: r for r in conn.execute(
        """SELECT r.* FROM scrape_runs r
           JOIN (SELECT source, MAX(run_id) AS run_id FROM scrape_runs GROUP BY source) last
             USING (source, run_id)"""
    )}
    out = []
    for name in sorted(REGISTRY.keys() | rows.keys()):
        r = rows.get(name)
        ok = conn.execute(
            "SELECT finished_at FROM scrape_runs WHERE source=? AND ok=1 ORDER BY run_id DESC LIMIT 1",
            (name,),
        ).fetchone()
        out.append({
            "source": name,
            "enabled": name in REGISTRY and REGISTRY[name].enabled,
            "last_run": r and {
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "ok": None if r["ok"] is None else bool(r["ok"]),
                "error": r["error"],
                "counts": {"courses": r["courses"], "items": r["items"], "grades": r["grades"]},
            },
            "last_success_at": ok["finished_at"] if ok else None,
            "login_command": _login_command(name, r and r["error"]),
        })
    return out


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
    fresher data than `GET /sources` shows; the data refreshes on its own every couple of
    hours."""
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
