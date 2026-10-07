"""SQLite storage. The scraper writes; the API only reads.

Each record is stored as its full JSON document plus a few indexed columns used for
filtering. When a source finishes a successful scrape, anything from that source that
wasn't seen in the run is marked inactive (it was deleted/hidden upstream) rather than
dropped, so history is kept.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config
from .models import Course, Grade, Item, ScrapeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    source TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT,
    data TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS items (
    source TEXT NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    course_id TEXT,
    course_name TEXT,
    title TEXT,
    status TEXT,
    due_at TEXT,
    posted_at TEXT,
    data TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS items_due ON items (due_at);
CREATE INDEX IF NOT EXISTS items_posted ON items (posted_at);
CREATE TABLE IF NOT EXISTS grades (
    source TEXT NOT NULL,
    id TEXT NOT NULL,
    course_name TEXT,
    term TEXT,
    data TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS scrape_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    error TEXT,
    courses INTEGER,
    items INTEGER,
    grades INTEGER,
    log TEXT  -- a failed run's trail of log lines and browser activity (scraper/trail.py)
);
-- Manual sync requests from the API. `sources` is a JSON list, or NULL for every enabled
-- source. The scraper sets started_at when a run picks one up, then finished_at and ok.
CREATE TABLE IF NOT EXISTS sync_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sources TEXT,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    ok INTEGER,
    error TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # treat naive datetimes as local time
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = path or config.DB_PATH
    if readonly:
        # Not `mode=ro`: in WAL mode a reader must be able to create the -wal/-shm files,
        # which the scraper removes when it exits. query_only still blocks all writes.
        # check_same_thread=False: FastAPI may open this in one threadpool thread and use it
        # in another. Each request gets its own connection, so it's never shared concurrently.
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only=ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")  # lets the API read while the scraper writes
        conn.executescript(SCHEMA)
        _migrate(conn)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Columns added after a table was first created."""
    if "log" not in {r[1] for r in conn.execute("PRAGMA table_info(scrape_runs)")}:
        conn.execute("ALTER TABLE scrape_runs ADD COLUMN log TEXT")


# Failed runs keep their trail (`log`) for this many of each source's latest runs.
KEEP_RUN_LOGS = 20


def init_db(path: Path | None = None) -> None:
    with connect(path):
        pass


def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (source, started_at) VALUES (?, ?)", (source, now_iso())
    )
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    result: ScrapeResult | None,
    error: str | None = None,
    log: str | None = None,
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, ok=?, error=?, courses=?, items=?, grades=?, log=? "
        "WHERE run_id=?",
        (
            now_iso(),
            int(error is None),
            error,
            len(result.courses) if result else None,
            len(result.items) if result else None,
            len(result.grades) if result else None,
            log,
            run_id,
        ),
    )
    conn.execute(
        """UPDATE scrape_runs SET log=NULL WHERE log IS NOT NULL AND source=?1 AND run_id NOT IN
             (SELECT run_id FROM scrape_runs WHERE source=?1 ORDER BY run_id DESC LIMIT ?2)""",
        (conn.execute("SELECT source FROM scrape_runs WHERE run_id=?", (run_id,)).fetchone()[0],
         KEEP_RUN_LOGS),
    )
    conn.commit()


def save_result(conn: sqlite3.Connection, source: str, result: ScrapeResult) -> None:
    """Upsert everything from one successful source scrape and retire what disappeared."""
    ts = now_iso()

    for c in result.courses:
        conn.execute(
            """INSERT INTO courses (source, id, name, data, active, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT (source, id) DO UPDATE SET
                 name=excluded.name, data=excluded.data, active=1, last_seen_at=excluded.last_seen_at""",
            (c.source, c.id, c.name, c.model_dump_json(), ts, ts),
        )

    for i in result.items:
        conn.execute(
            """INSERT INTO items (source, id, kind, course_id, course_name, title, status,
                                  due_at, posted_at, data, active, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT (source, id) DO UPDATE SET
                 kind=excluded.kind, course_id=excluded.course_id,
                 course_name=excluded.course_name, title=excluded.title,
                 status=excluded.status, due_at=excluded.due_at, posted_at=excluded.posted_at,
                 data=excluded.data, active=1, last_seen_at=excluded.last_seen_at""",
            (
                i.source, i.id, i.kind.value, i.course_id, i.course_name, i.title, i.status,
                _iso(i.due_at), _iso(i.posted_at), i.model_dump_json(), ts, ts,
            ),
        )

    for g in result.grades:
        conn.execute(
            """INSERT INTO grades (source, id, course_name, term, data, active, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT (source, id) DO UPDATE SET
                 course_name=excluded.course_name, term=excluded.term, data=excluded.data,
                 active=1, last_seen_at=excluded.last_seen_at""",
            (g.source, g.id, g.course_name, g.term, g.model_dump_json(), ts, ts),
        )

    for table in ("courses", "items", "grades"):
        conn.execute(
            f"UPDATE {table} SET active=0 WHERE source=? AND last_seen_at<?", (source, ts)
        )
    conn.commit()


def request_sync(conn: sqlite3.Connection, sources: list[str] | None) -> tuple[int, bool]:
    """Queue a sync of `sources` (None: every enabled source). Returns (request_id, created).
    A request already waiting that covers the same sources is returned instead of a new one."""
    for r in conn.execute("SELECT request_id, sources FROM sync_requests WHERE started_at IS NULL"):
        if r["sources"] is None or (sources is not None and set(sources) <= set(json.loads(r["sources"]))):
            return r["request_id"], False
    cur = conn.execute(
        "INSERT INTO sync_requests (sources, requested_at) VALUES (?, ?)",
        (None if sources is None else json.dumps(sorted(set(sources))), now_iso()),
    )
    conn.commit()
    return cur.lastrowid, True


def pending_sync_requests(conn: sqlite3.Connection) -> dict[int, list[str] | None]:
    """Requests no run has picked up yet: request_id -> sources (None: every enabled one)."""
    rows = conn.execute(
        "SELECT request_id, sources FROM sync_requests WHERE started_at IS NULL ORDER BY request_id"
    )
    return {r["request_id"]: None if r["sources"] is None else json.loads(r["sources"]) for r in rows}


def start_sync_requests(conn: sqlite3.Connection, request_ids: list[int]) -> None:
    conn.executemany(
        "UPDATE sync_requests SET started_at=? WHERE request_id=?",
        [(now_iso(), i) for i in request_ids],
    )
    conn.commit()


def finish_sync_request(conn: sqlite3.Connection, request_id: int, error: str | None = None) -> None:
    conn.execute(
        "UPDATE sync_requests SET finished_at=?, ok=?, error=? WHERE request_id=?",
        (now_iso(), int(error is None), error, request_id),
    )
    conn.commit()


def abandon_sync_requests(conn: sqlite3.Connection) -> None:
    """Fail requests a run started but never finished. Only call this while holding the run
    lock: then no other run is in progress, so that run must have died."""
    conn.execute(
        "UPDATE sync_requests SET finished_at=?, ok=0, error='Interrupted: the scraper stopped mid-run' "
        "WHERE started_at IS NOT NULL AND finished_at IS NULL",
        (now_iso(),),
    )
    conn.commit()


def clear_sync_requests(conn: sqlite3.Connection) -> int:
    """Delete every sync request except running ones (a run still reports on those).
    Pending ones are cancelled. Returns how many were deleted."""
    n = conn.execute(
        "DELETE FROM sync_requests WHERE started_at IS NULL OR finished_at IS NOT NULL"
    ).rowcount
    conn.commit()
    return n


def run_to_dict(row: sqlite3.Row, with_log: bool = False) -> dict[str, Any]:
    log = row["log"] if "log" in row.keys() else None  # older databases lack the column
    out = {
        "run_id": row["run_id"],
        "source": row["source"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "ok": None if row["ok"] is None else bool(row["ok"]),
        "error": row["error"],
        "counts": {"courses": row["courses"], "items": row["items"], "grades": row["grades"]},
        "has_log": log is not None,
    }
    if with_log:
        out["log"] = log
    return out


def sync_request_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    if row["finished_at"]:
        status = "done" if row["ok"] else "failed"
    else:
        status = "running" if row["started_at"] else "pending"
    return {
        "request_id": row["request_id"],
        "sources": None if row["sources"] is None else json.loads(row["sources"]),
        "status": status,
        "requested_at": row["requested_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
    }


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = json.loads(row["data"])
    data["active"] = bool(row["active"])
    data["first_seen_at"] = row["first_seen_at"]
    data["last_seen_at"] = row["last_seen_at"]
    return data


__all__ = [
    "Course", "Grade", "Item", "ScrapeResult", "connect", "init_db", "start_run",
    "finish_run", "save_result", "row_to_dict", "now_iso", "request_sync",
    "pending_sync_requests", "start_sync_requests", "finish_sync_request",
    "abandon_sync_requests", "sync_request_to_dict", "clear_sync_requests", "run_to_dict",
]
