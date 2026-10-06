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
    grades INTEGER
);
-- Attachment downloads, requested through the API and fetched by the files worker.
-- See lifeapi/files.py.
CREATE TABLE IF NOT EXISTS files (
    key TEXT PRIMARY KEY,          -- "<google id>.<format>"
    google_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    format TEXT NOT NULL,
    title TEXT,
    source_url TEXT,
    status TEXT NOT NULL,          -- pending, downloading, ready, failed, evicted
    error TEXT,
    filename TEXT,
    content_type TEXT,
    size INTEGER,
    requested_at TEXT NOT NULL,
    finished_at TEXT,
    last_accessed_at TEXT
);
CREATE INDEX IF NOT EXISTS files_status ON files (status);
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
        # The scraper, the files worker and the API's file requests all write, so wait out
        # each other's transactions. check_same_thread=False: see the readonly branch.
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")  # lets the API read while the scraper writes
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, ok=?, error=?, courses=?, items=?, grades=? "
        "WHERE run_id=?",
        (
            now_iso(),
            int(error is None),
            error,
            len(result.courses) if result else None,
            len(result.items) if result else None,
            len(result.grades) if result else None,
            run_id,
        ),
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


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = json.loads(row["data"])
    data["active"] = bool(row["active"])
    data["first_seen_at"] = row["first_seen_at"]
    data["last_seen_at"] = row["last_seen_at"]
    return data


__all__ = [
    "Course", "Grade", "Item", "ScrapeResult", "connect", "init_db", "start_run",
    "finish_run", "save_result", "row_to_dict", "now_iso",
]
