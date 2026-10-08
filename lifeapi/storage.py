"""SQLite storage. The scraper writes; the API only reads, apart from sync requests, fetch
schedules and browser settings.

Each record is stored as its full JSON document plus a few indexed columns used for
filtering. When a source finishes a successful full scrape, anything from that source that
wasn't seen in the run is marked inactive (it was deleted/hidden upstream) rather than
dropped, so history is kept. Records hold only their latest values; every change to a
grade-related value is also appended to `history`, which is never pruned. Scrape runs are
pruned after KEEP_RUNS_DAYS.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Iterator

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
    log TEXT,  -- a failed run's trail of log lines and browser activity (scraper/trail.py)
    partial TEXT,  -- the partial fetch this run did (`Source.partials`); NULL for a full one
    failure TEXT  -- whose problem a failed run is: 'scraper', 'site' or 'login' (runner.failure_kind)
);
CREATE INDEX IF NOT EXISTS scrape_runs_source ON scrape_runs (source, run_id);
-- Every change to a grade-related value, kept forever (see `_history_values`): a grade's
-- letter, percent, GPA and categories (`kind` grade), each of its scored assignments
-- (`entry`, keyed by `entry` within grade `id`), and an item's status and score (`item`).
-- `value` is the JSON of the tracked fields; a row is added only when it differs from the
-- latest row for the same record.
CREATE TABLE IF NOT EXISTS history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    entry TEXT,
    label TEXT,
    value TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_record ON history (source, kind, id, entry);
CREATE INDEX IF NOT EXISTS history_recorded ON history (recorded_at);
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
-- Fetch schedules set through the API. A source without a row is fetched in full every
-- LIFEAPI_SCRAPE_INTERVAL. With `partial` (one of the source's `Source.partials`), every
-- `full_every`th fetch is full and the rest are that partial fetch.
CREATE TABLE IF NOT EXISTS schedules (
    source TEXT PRIMARY KEY,
    interval_minutes INTEGER NOT NULL,
    partial TEXT,
    full_every INTEGER,
    updated_at TEXT NOT NULL
);
-- Per-source browser settings set through the API. A source without a row uses its own
-- default (`Source.headed`).
CREATE TABLE IF NOT EXISTS browser_settings (
    source TEXT PRIMARY KEY,
    headed INTEGER NOT NULL,
    updated_at TEXT NOT NULL
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
    columns = {r[1] for r in conn.execute("PRAGMA table_info(scrape_runs)")}
    for name in ("log", "partial", "failure"):
        if name not in columns:
            conn.execute(f"ALTER TABLE scrape_runs ADD COLUMN {name} TEXT")


# Failed runs keep their trail (`log`) for this many of each source's latest runs.
KEEP_RUN_LOGS = 20
# Scrape runs and finished sync requests older than this are deleted (`prune`), except
# each source's latest run, latest full run and latest successful full run, which
# schedules and /sources are worked out from.
KEEP_RUNS_DAYS = 180


def init_db(path: Path | None = None) -> None:
    with connect(path):
        pass


def start_run(conn: sqlite3.Connection, source: str, partial: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (source, started_at, partial) VALUES (?, ?, ?)",
        (source, now_iso(), partial),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    result: ScrapeResult | None,
    error: str | None = None,
    failure: str | None = None,
    log: str | None = None,
) -> None:
    """Record how a run ended: `result` if it succeeded, otherwise `error`, and `failure`
    for whose problem that is ("scraper", "site" or "login"; see `runner.failure_kind`)."""
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, ok=?, error=?, failure=?, courses=?, items=?, "
        "grades=?, log=? WHERE run_id=?",
        (
            now_iso(),
            int(error is None),
            error,
            failure if error is not None else None,
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


def prune(conn: sqlite3.Connection) -> int:
    """Delete scrape runs and finished sync requests older than KEEP_RUNS_DAYS, keeping
    the runs `next_fetch` and /sources need. Returns how many runs were deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_RUNS_DAYS)).isoformat(timespec="seconds")
    n = conn.execute(
        """DELETE FROM scrape_runs WHERE started_at<? AND run_id NOT IN (
             SELECT MAX(run_id) FROM scrape_runs GROUP BY source
             UNION SELECT MAX(run_id) FROM scrape_runs WHERE partial IS NULL GROUP BY source
             UNION SELECT MAX(run_id) FROM scrape_runs WHERE partial IS NULL AND ok=1
                   GROUP BY source)""",
        (cutoff,),
    ).rowcount
    conn.execute("DELETE FROM sync_requests WHERE finished_at IS NOT NULL AND requested_at<?",
                 (cutoff,))
    conn.commit()
    return n


def _compact(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v not in (None, [], {})}


def _history_values(result: ScrapeResult) -> Iterator[tuple[str, str, str | None, str, dict[str, Any]]]:
    """(kind, id, entry, label, tracked fields) for every grade-related value in `result`.
    Fields that are null or empty are left out, so adding one to a model changes nothing
    until it has a value."""
    for g in result.grades:
        label = g.course_name if g.gpa is not None else " · ".join(
            filter(None, (g.course_name, g.term, g.task)))
        yield "grade", g.id, None, label, _compact({
            "letter": g.letter, "percent": g.percent, "gpa": g.gpa,
            "points_earned": g.extra.get("points_earned"),
            "points_possible": g.extra.get("points_possible"),
            "term_gpa": g.extra.get("term_gpa"),
            "categories": [_compact({k: c.get(k) for k in
                                     ("name", "letter", "percent", "points_earned", "points_possible")})
                           for c in g.categories],
        })
        for e in g.entries:
            yield "entry", g.id, e.url or f"{e.name}|{e.due_at}", e.name, _compact({
                "score": e.score, "points_earned": e.points_earned,
                "points_possible": e.points_possible, "percent": e.percent, "flags": e.flags,
                "comments": e.comments,
            })
    for i in result.items:
        yield "item", i.id, None, i.title, _compact({
            "status": i.status, "score": i.score, "points_possible": i.points_possible,
        })


def _record_history(conn: sqlite3.Connection, source: str, result: ScrapeResult, ts: str) -> None:
    latest = {
        (r["kind"], r["id"], r["entry"]): r["value"]
        for r in conn.execute(
            """SELECT kind, id, entry, value FROM history WHERE history_id IN
                 (SELECT MAX(history_id) FROM history WHERE source=? GROUP BY kind, id, entry)""",
            (source,),
        )
    }
    rows = []
    for kind, id_, entry, label, fields in _history_values(result):
        value = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        before = latest.get((kind, id_, entry))
        if value != before and (before is not None or fields):  # nothing to start from
            rows.append((source, kind, id_, entry, label, value, ts))
    conn.executemany(
        "INSERT INTO history (source, kind, id, entry, label, value, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def save_result(conn: sqlite3.Connection, source: str, result: ScrapeResult,
                partial: bool = False) -> None:
    """Upsert everything from one successful source scrape, record grade-related changes in
    `history`, and retire what disappeared. A partial scrape saw only part of the source, so
    it retires nothing; the next full one does."""
    ts = now_iso()
    _record_history(conn, source, result, ts)

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

    if not partial:
        for table in ("courses", "items", "grades"):
            conn.execute(
                f"UPDATE {table} SET active=0 WHERE source=? AND last_seen_at<?", (source, ts)
            )
    conn.commit()


def get_schedule(conn: sqlite3.Connection, source: str, partials: Collection[str]) -> dict[str, Any]:
    """`source`'s fetch schedule: the one set through the API, else the default (in full
    every LIFEAPI_SCRAPE_INTERVAL). `partials` are the ones the source has; a stored partial
    it no longer has reads as none."""
    row = conn.execute("SELECT * FROM schedules WHERE source=?", (source,)).fetchone()
    if row is None:
        return {"interval_minutes": config.SCRAPE_INTERVAL_MINUTES, "partial": None,
                "full_every": None, "default": True}
    partial = row["partial"] if row["partial"] in partials else None
    return {"interval_minutes": row["interval_minutes"], "partial": partial,
            "full_every": row["full_every"] if partial else None, "default": False}


def set_schedule(conn: sqlite3.Connection, source: str, interval_minutes: int,
                 partial: str | None = None, full_every: int | None = None) -> None:
    conn.execute(
        """INSERT INTO schedules (source, interval_minutes, partial, full_every, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (source) DO UPDATE SET
             interval_minutes=excluded.interval_minutes, partial=excluded.partial,
             full_every=excluded.full_every, updated_at=excluded.updated_at""",
        (source, interval_minutes, partial, full_every, now_iso()),
    )
    conn.commit()


def reset_schedule(conn: sqlite3.Connection, source: str) -> None:
    conn.execute("DELETE FROM schedules WHERE source=?", (source,))
    conn.commit()


def last_full_run(conn: sqlite3.Connection, source: str) -> datetime | None:
    """When `source`'s last successful full run started, or None if it hasn't had one."""
    row = conn.execute(
        "SELECT started_at FROM scrape_runs WHERE source=? AND ok=1 AND partial IS NULL"
        " ORDER BY run_id DESC LIMIT 1", (source,)
    ).fetchone()
    return datetime.fromisoformat(row["started_at"]) if row else None


def next_fetch(conn: sqlite3.Connection, source: str,
               schedule: dict[str, Any]) -> tuple[datetime | None, str | None]:
    """When `schedule` next fetches `source`, and which partial fetch that is (None: full).
    The time is None if the source has never run (so it's due now). Every run counts,
    scheduled or manual, failed or not: the interval runs from the last one's start, and
    the `full_every`th run after a full one is full."""
    last = conn.execute(
        "SELECT started_at FROM scrape_runs WHERE source=? ORDER BY run_id DESC LIMIT 1", (source,)
    ).fetchone()
    if last is None:
        return None, None
    at = datetime.fromisoformat(last["started_at"]) + timedelta(minutes=schedule["interval_minutes"])
    if not schedule["partial"]:
        return at, None
    last_full = conn.execute(
        "SELECT MAX(run_id) FROM scrape_runs WHERE source=? AND partial IS NULL", (source,)
    ).fetchone()[0]
    if last_full is None:
        return at, None
    since = conn.execute(
        "SELECT COUNT(*) FROM scrape_runs WHERE source=? AND run_id>?", (source, last_full)
    ).fetchone()[0]
    return at, schedule["partial"] if since < schedule["full_every"] - 1 else None


def get_browser(conn: sqlite3.Connection, source: str, default_headed: bool) -> dict[str, Any]:
    """How `source`'s browser runs: the setting made through the API, else the source's
    default (`default_headed`)."""
    row = conn.execute("SELECT headed FROM browser_settings WHERE source=?", (source,)).fetchone()
    if row is None:
        return {"headed": default_headed, "default": True}
    return {"headed": bool(row["headed"]), "default": False}


def set_browser(conn: sqlite3.Connection, source: str, headed: bool) -> None:
    conn.execute(
        """INSERT INTO browser_settings (source, headed, updated_at) VALUES (?, ?, ?)
           ON CONFLICT (source) DO UPDATE SET
             headed=excluded.headed, updated_at=excluded.updated_at""",
        (source, int(headed), now_iso()),
    )
    conn.commit()


def reset_browser(conn: sqlite3.Connection, source: str) -> None:
    conn.execute("DELETE FROM browser_settings WHERE source=?", (source,))
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
    failure = row["failure"] if "failure" in row.keys() else None
    if row["ok"] == 0 and failure is None:  # recorded before runs had a failure kind
        failure = "login" if (row["error"] or "").startswith("LoginError:") else "scraper"
    out = {
        "run_id": row["run_id"],
        "source": row["source"],
        "partial": row["partial"] if "partial" in row.keys() else None,
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "ok": None if row["ok"] is None else bool(row["ok"]),
        "error": row["error"],
        "failure": failure,
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
    "get_schedule", "set_schedule", "reset_schedule", "next_fetch", "get_browser",
    "set_browser", "reset_browser",
]
