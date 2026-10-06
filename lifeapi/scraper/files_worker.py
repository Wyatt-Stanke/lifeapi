"""Files worker: downloads the attachment files requested through the API (lifeapi/files.py).

    python -m lifeapi.scraper.files_worker          # poll for requests forever
    python -m lifeapi.scraper.files_worker --once   # download what's queued, then exit

Downloads go through the scraper's signed-in browser profile, so Drive and Docs see the
student's own session. The worker opens the browser only while requests are queued; the
profile lock (browser.py) makes it wait for a running scrape, and vice versa.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import mimetypes
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from patchright.async_api import BrowserContext, Download, Error as PlaywrightError, Page

from .. import config, files, storage
from .auth.google import google_login, on_google_login
from .browser import browser_context, dump_debug

log = logging.getLogger("lifeapi.files")

POLL_SECONDS = 3
DOWNLOAD_TIMEOUT = 30 * 60
# Touched on every poll; deploy/container/healthcheck.sh reads it.
HEARTBEAT = Path(os.getenv("LIFEAPI_FILES_HEARTBEAT", "/tmp/files-worker.heartbeat"))
# Pick the scraper's account even when the profile holds several Google accounts.
AUTHUSER = os.getenv("GOOGLE_USERNAME") or "0"
# Drive's own Download button exports a Docs editors file to its Office equivalent. A
# drive_file link that turns out to be one of those gets the same treatment.
OFFICE_FORMAT = {"google_doc": "docx", "google_slides": "pptx", "google_sheet": "xlsx"}


class FileError(RuntimeError):
    """A download failed for a reason worth showing the user as-is."""


async def _navigate_for_download(page: Page, url: str) -> Download | None:
    """Go to `url`. Returns the download it starts, or None if it loaded a web page."""
    waiter = asyncio.ensure_future(page.wait_for_event("download", timeout=60_000))
    try:
        await page.goto(url)
    except PlaywrightError as e:
        # Navigating straight to a file aborts the navigation and starts a download.
        if "Download is starting" in str(e) or "net::ERR_ABORTED" in str(e):
            return await waiter
        waiter.cancel()
        raise
    done, _ = await asyncio.wait({waiter}, timeout=2)
    if done:
        return waiter.result()
    waiter.cancel()
    return None


async def _explain(page: Page, row: dict[str, Any]) -> str:
    title = await page.title()
    body = await page.inner_text("body") if await page.locator("body").count() else ""
    if "403" in title or "Access Denied" in title or "do not have access" in body:
        return "No access: the file was deleted, or it isn't shared with this Google account."
    await dump_debug(page, f"files-{row['key']}")
    return f"Google showed a page instead of the file ({title!r}). It may not allow downloads."


async def _resolve_drive_kind(page: Page, google_id: str) -> str | None:
    """The real kind of a drive_file whose download didn't start: Drive's viewer redirects
    Docs editors files to their editor."""
    await page.goto(f"https://drive.google.com/file/d/{google_id}/view?authuser={AUTHUSER}")
    await page.wait_for_timeout(2000)
    target = files.classify(page.url)
    return target[0] if target and target[0] != "drive_file" else None


async def _fetch(ctx: BrowserContext, row: dict[str, Any], dest: Path) -> str:
    """Download `row` to `dest`. Returns Google's suggested filename."""
    kind, fmt = row["kind"], row["format"]
    page = await ctx.new_page()
    try:
        for _ in range(3):
            url = files.download_url(kind, row["google_id"], fmt, AUTHUSER)
            download = await _navigate_for_download(page, url)
            if download is None and on_google_login(page):
                await google_login(page)
                continue
            if download is None and await page.locator("#download-form").count():
                # Files too large for Drive's virus scan get a "Download anyway" form.
                waiter = asyncio.ensure_future(page.wait_for_event("download", timeout=60_000))
                await page.locator("#download-form [type=submit]").first.click()
                download = await waiter
            if download is None and kind == "drive_file":
                if real := await _resolve_drive_kind(page, row["google_id"]):
                    log.info("%s is a %s file; exporting as %s", row["key"], real, OFFICE_FORMAT[real])
                    kind, fmt = real, OFFICE_FORMAT[real]
                    continue
            if download is None:
                raise FileError(await _explain(page, row))
            try:
                await asyncio.wait_for(download.save_as(dest), DOWNLOAD_TIMEOUT)
            except asyncio.TimeoutError:
                await download.cancel()
                raise FileError(f"Download took longer than {DOWNLOAD_TIMEOUT // 60} minutes")
            return download.suggested_filename
        raise FileError("Google kept asking to sign in")
    finally:
        await page.close()


def _claim(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM files WHERE status='pending' ORDER BY requested_at LIMIT 1"
    ).fetchone()
    if not row:
        return None
    conn.execute("UPDATE files SET status='downloading' WHERE key=?", (row["key"],))
    conn.commit()
    return dict(row)


def _finish(conn: sqlite3.Connection, file_key: str, **fields: Any) -> None:
    fields["finished_at"] = storage.now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE files SET {sets} WHERE key=?", (*fields.values(), file_key))
    conn.commit()


async def _process(ctx: BrowserContext, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    file_key, limit = row["key"], config.ATTACHMENT_STORAGE_BYTES
    tmp = config.FILES_DIR / ".tmp" / file_key
    tmp.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s (%s)", file_key, row["title"] or row["kind"])
    try:
        filename = await _fetch(ctx, row, tmp)
        size = tmp.stat().st_size
        if size > limit:
            raise FileError(
                f"The file is {size / 1e6:.1f} MB, more than the whole attachment storage "
                f"limit ({limit / 1e9:g} GB, LIFEAPI_ATTACHMENT_STORAGE_GB)"
            )
        if evicted := files.make_room(conn, size, keep=file_key):
            log.info("Evicted %d file(s) to make room: %s", len(evicted), ", ".join(evicted))
        tmp.replace(files.path(file_key))
        _finish(
            conn, file_key, status="ready", error=None, filename=filename, size=size,
            content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            last_accessed_at=None,
        )
        log.info("Downloaded %s: %s, %d bytes", file_key, filename, size)
    except Exception as e:
        error = str(e) if isinstance(e, FileError) else f"{type(e).__name__}: {e}"
        log.error("%s failed: %s", file_key, error)
        _finish(conn, file_key, status="failed", error=error)
    finally:
        tmp.unlink(missing_ok=True)


def _has_pending(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM files WHERE status='pending' LIMIT 1").fetchone() is not None


async def run(once: bool = False) -> None:
    config.FILES_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(config.FILES_DIR / ".tmp", ignore_errors=True)  # partial downloads
    with storage.connect() as conn:
        # Anything still "downloading" was interrupted by a restart.
        conn.execute("UPDATE files SET status='pending' WHERE status='downloading'")
        conn.commit()
        while True:
            HEARTBEAT.touch()
            if _has_pending(conn):
                async with browser_context() as ctx:
                    while row := _claim(conn):
                        await _process(ctx, conn, row)
                        HEARTBEAT.touch()
            if once:
                return
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m lifeapi.scraper.files_worker")
    parser.add_argument("--once", action="store_true", help="download what's queued, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    asyncio.run(run(once=args.once))


if __name__ == "__main__":
    main()
