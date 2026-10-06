"""Read-only HTTP API over the scraped data. Runs independently of the scraper; it only
reads the SQLite database the scraper writes."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse

from .. import config, files, storage
from ..models import ItemKind

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


def db_rw() -> Iterator[sqlite3.Connection]:
    """For the attachment-file endpoints, the only ones that write (requests, access times)."""
    if not config.DB_PATH.exists():
        raise HTTPException(503, "No data yet; run the scraper first")
    with storage.connect() as conn:
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
    """Each source's most recent scrape run and its last successful one."""
    rows = conn.execute(
        """SELECT r.* FROM scrape_runs r
           JOIN (SELECT source, MAX(run_id) AS run_id FROM scrape_runs GROUP BY source) last
             USING (source, run_id)
           ORDER BY source"""
    ).fetchall()
    out = []
    for r in rows:
        ok = conn.execute(
            "SELECT finished_at FROM scrape_runs WHERE source=? AND ok=1 ORDER BY run_id DESC LIMIT 1",
            (r["source"],),
        ).fetchone()
        out.append({
            "source": r["source"],
            "last_run": {
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "ok": None if r["ok"] is None else bool(r["ok"]),
                "error": r["error"],
                "counts": {"courses": r["courses"], "items": r["items"], "grades": r["grades"]},
            },
            "last_success_at": ok["finished_at"] if ok else None,
        })
    return out


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


# -- attachment files ---------------------------------------------------------------------
# Google Drive / Docs / Slides / Sheets attachments, downloaded on request by the files
# worker. See lifeapi/files.py.

FORMAT_HELP = (
    "Google Docs: pdf, txt, md, docx. Google Slides: pdf, txt, pptx. "
    "Google Sheets: xlsx, csv (first sheet only), pdf. Drive files: original."
)


def _file_out(f: dict[str, Any]) -> dict[str, Any]:
    f = dict(f)
    f["content_url"] = f"/files/{f['key']}/content" if f["status"] == "ready" else None
    return f


def _attachments(conn: sqlite3.Connection, source: str, item_id: str) -> list[dict[str, Any]]:
    row = conn.execute("SELECT * FROM items WHERE source=? AND id=?", (source, item_id)).fetchone()
    if not row:
        raise HTTPException(404, "Item not found")
    atts = files.item_attachments(storage.row_to_dict(row))
    stored = files.for_google_ids(conn, [a["google_id"] for a in atts if a["google_id"]])
    for a in atts:
        a["files"] = {f["format"]: _file_out(f) for f in stored.get(a["google_id"], [])}
    return atts


@app.get("/items/{source}/{item_id}/attachments", dependencies=[Auth])
def item_attachments(
    source: str, item_id: str, conn: sqlite3.Connection = Depends(db_rw)
) -> list[dict[str, Any]]:
    """The item's attachments, submitted work and description links. Downloadable ones
    have a `google_id` and the `formats` they can be requested in; `files` holds the
    requests made so far, by format."""
    return _attachments(conn, source, item_id)


@app.post("/items/{source}/{item_id}/attachments/{google_id}/files", dependencies=[Auth], status_code=202)
def request_file(
    source: str,
    item_id: str,
    google_id: str,
    response: Response,
    format: str = Query(..., description=FORMAT_HELP),
    refresh: bool = Query(False, description="Download again even if a copy is ready"),
    conn: sqlite3.Connection = Depends(db_rw),
) -> dict[str, Any]:
    """Request an attachment as a file. Returns the file record: 202 while it's queued or
    downloading (poll `GET /files/{key}`), 200 once it's ready. Requesting a failed or
    evicted file queues it again."""
    att = next((a for a in _attachments(conn, source, item_id) if a["google_id"] == google_id), None)
    if not att:
        raise HTTPException(404, "No downloadable attachment with that google_id on this item")
    if format not in att["formats"]:
        raise HTTPException(
            422, f"{files.KIND_NAMES[att['kind']]} attachments can be requested as: "
                 f"{', '.join(att['formats'])}"
        )
    f = files.request(conn, att["kind"], google_id, format, att["title"], att["url"], refresh)
    if f["status"] == "ready":
        response.status_code = 200
    return _file_out(f)


@app.get("/files", dependencies=[Auth])
def list_files(
    status: str | None = Query(None, description="pending, downloading, ready, failed or evicted"),
    conn: sqlite3.Connection = Depends(db_rw),
) -> dict[str, Any]:
    """Every requested file, newest request first, and the storage used by ready files."""
    sql, args = "SELECT * FROM files", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    rows = conn.execute(sql + " ORDER BY requested_at DESC", args)
    return {"usage": files.usage(conn), "files": [_file_out(dict(r)) for r in rows]}


def _file(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    if not (f := files.get(conn, key)):
        raise HTTPException(404, "File not found")
    return f


@app.get("/files/{key}", dependencies=[Auth])
def get_file(key: str, conn: sqlite3.Connection = Depends(db_rw)) -> dict[str, Any]:
    return _file_out(_file(conn, key))


@app.get("/files/{key}/content", dependencies=[Auth])
def file_content(key: str, conn: sqlite3.Connection = Depends(db_rw)) -> FileResponse:
    f = _file(conn, key)
    if f["status"] != "ready" or not files.path(key).exists():
        detail = f"File is {f['status']}, not ready" + (f": {f['error']}" if f["error"] else "")
        raise HTTPException(409, detail)
    files.touch(conn, key)
    return FileResponse(files.path(key), media_type=f["content_type"], filename=f["filename"] or key)


@app.delete("/files/{key}", dependencies=[Auth], status_code=204)
def delete_file(key: str, conn: sqlite3.Connection = Depends(db_rw)) -> Response:
    f = _file(conn, key)
    if f["status"] == "downloading":
        raise HTTPException(409, "File is downloading; try again when it's done")
    files.path(key).unlink(missing_ok=True)
    conn.execute("DELETE FROM files WHERE key=?", (key,))
    conn.commit()
    return Response(status_code=204)
