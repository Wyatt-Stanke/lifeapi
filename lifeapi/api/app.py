"""HTTP API over the scraped data. Runs independently of the scraper and only reads the
SQLite database the scraper writes, except for queueing manual sync requests (`/sync`),
which the scraper picks up."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response

from .. import config, storage
from ..models import ItemKind
from ..scraper import sources as _sources  # noqa: F401  (registers all sources)
from ..scraper.base import REGISTRY

app = FastAPI(
    title="lifeapi",
    description="Schoolwork from every platform, in one place.",
    version="1.0.0",
)


def require_token(authorization: str | None = Header(default=None)) -> None:
    if config.API_TOKEN and authorization != f"Bearer {config.API_TOKEN}":
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/sources", dependencies=[Auth])
def sources(conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
    """Every registered source with its most recent scrape run (null if it has never run)
    and its last successful one."""
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
        })
    return out


# The sync endpoints write, so they open their own read-write connection (which also
# creates the table on a database the current scraper hasn't touched yet).

@app.post("/sync", dependencies=[Auth], status_code=202)
def request_sync(
    response: Response,
    source: list[str] | None = Query(None, description="Sources to sync (repeatable); omit for every enabled source"),
) -> dict[str, Any]:
    """Ask the scraper to sync now. It picks the request up within seconds if it's idle, or
    after the current run. If a waiting request already covers these sources, that one is
    returned (200) instead of queueing another (202). Poll `GET /sync/{request_id}`."""
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


@app.get("/sync", dependencies=[Auth])
def sync_requests(limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    """Recent sync requests, newest first. `status` is pending, running, done or failed."""
    with storage.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_requests ORDER BY request_id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [storage.sync_request_to_dict(r) for r in rows]


@app.get("/sync/{request_id}", dependencies=[Auth])
def sync_request(request_id: int) -> dict[str, Any]:
    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM sync_requests WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Sync request not found")
    return storage.sync_request_to_dict(row)


@app.get("/courses", dependencies=[Auth])
def courses(
    source: str | None = None,
    include_inactive: bool = False,
    conn: sqlite3.Connection = Depends(db),
) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM courses WHERE 1=1", []
    if source:
        sql += " AND source=?"
        args.append(source)
    if not include_inactive:
        sql += " AND active=1"
    sql += " ORDER BY source, name"
    return [storage.row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/items", dependencies=[Auth])
def items(
    source: list[str] | None = Query(None, description="Filter by source (repeatable)"),
    kind: list[ItemKind] | None = Query(None, description="Filter by kind (repeatable)"),
    course_id: str | None = None,
    status: list[str] | None = Query(None, description="e.g. assigned, missing, turned_in"),
    due_after: datetime | None = None,
    due_before: datetime | None = None,
    posted_after: datetime | None = None,
    q: str | None = Query(None, description="Case-insensitive search in title/course"),
    include_inactive: bool = False,
    order: str = Query("due", pattern="^(due|posted|seen)$"),
    limit: int = Query(200, le=2000),
    offset: int = 0,
    conn: sqlite3.Connection = Depends(db),
) -> list[dict[str, Any]]:
    """Assignments, announcements, materials, questions — from every source."""
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


@app.get("/items/upcoming", dependencies=[Auth])
def upcoming(
    days: int = Query(14, ge=1, le=365),
    include_done: bool = False,
    conn: sqlite3.Connection = Depends(db),
) -> list[dict[str, Any]]:
    """Work due between now and `days` from now, soonest first."""
    now = datetime.now(timezone.utc)
    sql = "SELECT * FROM items WHERE active=1 AND due_at>=? AND due_at<=?"
    if not include_done:
        sql += " AND (status IS NULL OR status NOT IN ('turned_in','completed','graded','done','returned','handed_in'))"
    sql += " ORDER BY due_at"
    return [storage.row_to_dict(r) for r in conn.execute(sql, (_iso(now), _iso(now + timedelta(days=days))))]


@app.get("/items/missing", dependencies=[Auth])
def missing(conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
    """Work past due that isn't marked done anywhere."""
    now = datetime.now(timezone.utc)
    sql = """SELECT * FROM items WHERE active=1 AND due_at<? AND
             (status IN ('missing','assigned') OR status IS NULL) AND kind!='announcement'
             ORDER BY due_at DESC"""
    return [storage.row_to_dict(r) for r in conn.execute(sql, (_iso(now),))]


@app.get("/announcements", dependencies=[Auth])
def announcements(
    days: int = Query(14, ge=1, le=365),
    source: str | None = None,
    conn: sqlite3.Connection = Depends(db),
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    sql, args = "SELECT * FROM items WHERE active=1 AND kind='announcement' AND posted_at>=?", [_iso(since)]
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY posted_at DESC"
    return [storage.row_to_dict(r) for r in conn.execute(sql, args)]


@app.get("/items/{source}/{item_id}", dependencies=[Auth])
def item(source: str, item_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM items WHERE source=? AND id=?", (source, item_id)).fetchone()
    if not row:
        raise HTTPException(404, "Item not found")
    return storage.row_to_dict(row)


@app.get("/grades", dependencies=[Auth])
def grades(
    source: str | None = None,
    term: str | None = None,
    include_inactive: bool = False,
    conn: sqlite3.Connection = Depends(db),
) -> list[dict[str, Any]]:
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
