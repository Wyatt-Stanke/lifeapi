"""Attachment files: Google Drive / Docs / Slides / Sheets attachments, downloaded on request.

The API queues a request (a `files` row with status `pending`). The files worker
(`python -m lifeapi.scraper.files_worker`) downloads it with the scraper's signed-in
browser, then marks it `ready`. Files are keyed by Google file ID + format, so the same
Doc attached to several items is stored once. Total size is capped at
`config.ATTACHMENT_STORAGE_BYTES`; the least recently used files are evicted to make room.

Status: pending -> downloading -> ready | failed. A ready file can become `evicted`.
Requesting a failed or evicted file again re-queues it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import config
from .storage import now_iso

# Format -> export URL template, per kind. `original` downloads the file as uploaded.
# Docs/Slides/Sheets formats are the editors' own File > Download exports.
_DOCS = "https://docs.google.com/{path}/d/{id}/export?format={fmt}"
FORMATS: dict[str, dict[str, str]] = {
    "google_doc": {f: _DOCS.replace("{path}", "document") for f in ("pdf", "txt", "md", "docx")},
    "google_slides": {f: _DOCS.replace("{path}", "presentation") for f in ("pdf", "txt", "pptx")},
    # csv exports only the first sheet (tab).
    "google_sheet": {f: _DOCS.replace("{path}", "spreadsheets") for f in ("xlsx", "csv", "pdf")},
    "drive_file": {"original": "https://drive.usercontent.google.com/download?id={id}&export=download"},
}
KIND_NAMES = {
    "google_doc": "Google Docs", "google_slides": "Google Slides",
    "google_sheet": "Google Sheets", "drive_file": "Drive file",
}

_ID = r"([\w-]{10,})"
_PATTERNS = [
    ("google_doc", re.compile(rf"docs\.google\.com/document/(?:u/\d+/)?d/{_ID}")),
    ("google_slides", re.compile(rf"docs\.google\.com/presentation/(?:u/\d+/)?d/{_ID}")),
    ("google_sheet", re.compile(rf"docs\.google\.com/spreadsheets/(?:u/\d+/)?d/{_ID}")),
    ("drive_file", re.compile(rf"drive\.google\.com/file/(?:u/\d+/)?d/{_ID}")),
    # open?id= / uc?id= links can point at any Drive file, including Docs editors files.
    # They're treated as plain files; the worker reports it if one turns out to be a Doc.
    ("drive_file", re.compile(rf"drive\.google\.com/(?:open|uc)\?(?:.*&)?id={_ID}")),
]


def classify(url: str | None) -> tuple[str, str] | None:
    """(kind, google_id) for a downloadable Google URL, else None. Folders, Forms, YouTube
    and ordinary links aren't downloadable."""
    for kind, pattern in _PATTERNS:
        if url and (m := pattern.search(url)):
            return kind, m.group(1)
    return None


def key(google_id: str, fmt: str) -> str:
    return f"{google_id}.{fmt}"


def path(file_key: str) -> Path:
    return config.FILES_DIR / file_key


def download_url(kind: str, google_id: str, fmt: str, authuser: str) -> str:
    return FORMATS[kind][fmt].format(id=google_id, fmt=fmt) + f"&authuser={authuser}"


def item_attachments(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Every attachment-like URL on an item: teacher attachments, the student's submitted
    work and links in the description. Downloadable ones get `kind`, `google_id` and
    `formats`; duplicates of the same Google file are dropped."""
    extra = item.get("extra") or {}
    candidates: Iterable[tuple[str, dict[str, Any]]] = [
        *(("attachment", a) for a in item.get("attachments") or []),
        *(("submitted_work", a) for a in extra.get("submitted_work") or []),
        *(("link", {"url": u}) for u in extra.get("links") or []),
    ]
    out, seen = [], set()
    for origin, a in candidates:
        target = classify(a.get("url"))
        if target and target in seen:
            continue
        if target:
            seen.add(target)
        kind, google_id = target or (None, None)
        out.append({
            "origin": origin,
            "title": a.get("title"),
            "url": a.get("url"),
            "type": a.get("type"),
            "kind": kind,
            "google_id": google_id,
            "formats": list(FORMATS[kind]) if kind else [],
        })
    return out


# -- the files table ----------------------------------------------------------------------

def get(conn: sqlite3.Connection, file_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM files WHERE key=?", (file_key,)).fetchone()
    return dict(row) if row else None


def for_google_ids(conn: sqlite3.Connection, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if ids:
        rows = conn.execute(
            f"SELECT * FROM files WHERE google_id IN ({','.join('?' * len(ids))})", ids
        )
        for r in rows:
            out.setdefault(r["google_id"], []).append(dict(r))
    return out


def request(
    conn: sqlite3.Connection, kind: str, google_id: str, fmt: str,
    title: str | None, source_url: str | None, refresh: bool = False,
) -> dict[str, Any]:
    """Queue a download unless it's already ready (or in progress). `refresh` re-downloads
    a ready file, e.g. after the Doc was edited."""
    file_key = key(google_id, fmt)
    existing = get(conn, file_key)
    if existing and existing["status"] in ("pending", "downloading"):
        return existing
    if existing and existing["status"] == "ready" and not refresh:
        return existing
    conn.execute(
        """INSERT INTO files (key, google_id, kind, format, title, source_url, status, requested_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
           ON CONFLICT (key) DO UPDATE SET status='pending', error=NULL, kind=excluded.kind,
             title=COALESCE(excluded.title, title), source_url=excluded.source_url,
             requested_at=excluded.requested_at""",
        (file_key, google_id, kind, fmt, title, source_url, now_iso()),
    )
    conn.commit()
    return get(conn, file_key)


def touch(conn: sqlite3.Connection, file_key: str) -> None:
    conn.execute("UPDATE files SET last_accessed_at=? WHERE key=?", (now_iso(), file_key))
    conn.commit()


def usage(conn: sqlite3.Connection) -> dict[str, int]:
    used = conn.execute("SELECT COALESCE(SUM(size), 0) FROM files WHERE status='ready'").fetchone()[0]
    return {"used_bytes": used, "limit_bytes": config.ATTACHMENT_STORAGE_BYTES}


def make_room(conn: sqlite3.Connection, needed: int, keep: str) -> list[str]:
    """Evict the least recently used ready files (never `keep`) until `needed` more bytes
    fit under the limit. Returns the evicted keys."""
    evicted = []
    free = config.ATTACHMENT_STORAGE_BYTES - usage(conn)["used_bytes"]
    rows = conn.execute(
        """SELECT key, size FROM files WHERE status='ready' AND key!=?
           ORDER BY COALESCE(last_accessed_at, finished_at)""",
        (keep,),
    ).fetchall()
    for r in rows:
        if free >= needed:
            break
        path(r["key"]).unlink(missing_ok=True)
        conn.execute("UPDATE files SET status='evicted' WHERE key=?", (r["key"],))
        free += r["size"] or 0
        evicted.append(r["key"])
    conn.commit()
    return evicted
