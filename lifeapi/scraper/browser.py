"""Stealth browser session built on patchright (an undetected fork of Playwright).

A single persistent profile is shared by every source, so one Google login is reused by
Classroom, Clever, Albert and Infinite Campus, and sessions survive between runs (which
keeps the number of fresh logins — and the chance of security challenges — low).
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import AsyncIterator

from patchright.async_api import BrowserContext, Page, async_playwright

from .. import config

log = logging.getLogger(__name__)


@asynccontextmanager
async def _profile_lock(profile_dir: Path) -> AsyncIterator[None]:
    """Only one process may use the profile at a time (the scraper, the files worker, a
    login session). Others wait here instead of failing on Chrome's own profile lock."""
    with open(profile_dir.with_name(profile_dir.name + ".lock"), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Browser profile in use by another process; waiting for it")
            await asyncio.to_thread(fcntl.flock, f, fcntl.LOCK_EX)
        # Chrome's Singleton* files name the host that last held the profile. A recreated
        # container has a new hostname, and Chrome then refuses the profile as "in use on
        # another computer". We hold the lock, so they're stale.
        for p in profile_dir.glob("Singleton*"):
            p.unlink(missing_ok=True)
        _clear_download_history(profile_dir)
        yield


def _clear_download_history(profile_dir: Path) -> None:
    """Chrome (154, headless) crashes on a session's first download when its history lists
    finished downloads whose files are gone. Here they always are: Playwright saves
    downloads to a temp dir it deletes on close. So after one session that downloaded a
    file (the files worker), every later download crashed the browser. Nothing uses the
    download history, so it's emptied while Chrome isn't running."""
    history = profile_dir / "Default" / "History"
    if not history.exists():
        return
    try:
        with closing(sqlite3.connect(history, timeout=5)) as conn, conn:
            for table in ("downloads", "downloads_url_chains", "downloads_slices"):
                conn.execute(f"DELETE FROM {table}")
    except sqlite3.Error as e:
        log.warning("Could not clear Chrome's download history: %s", e)


@asynccontextmanager
async def browser_context(
    headless: bool | None = None, profile_dir: Path | None = None
) -> AsyncIterator[BrowserContext]:
    profile_dir = profile_dir or config.BROWSER_PROFILE_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)
    async with _profile_lock(profile_dir), async_playwright() as pw:
        # patchright's recommended stealth setup: real Chrome, persistent profile,
        # no custom viewport/user agent (those are fingerprintable).
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=config.BROWSER_CHANNEL,
            headless=config.HEADLESS if headless is None else headless,
            no_viewport=True,
            locale="en-US",
            timezone_id="America/New_York",
            # Sources read several pages in parallel tabs; without these, Chrome stops
            # rendering background tabs and their content never appears.
            args=[
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
            ],
        )
        ctx.set_default_timeout(30_000)
        try:
            yield ctx
        finally:
            await ctx.close()


async def dump_debug(page: Page, name: str) -> None:
    """Save a screenshot + HTML of the current page to help fix broken selectors."""
    config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stem = config.DEBUG_DIR / re.sub(r"[^\w.-]+", "_", name)
    try:
        await page.screenshot(path=f"{stem}.png", full_page=True)
        Path(f"{stem}.html").write_text(await page.content())
        log.info("Saved debug snapshot to %s.{png,html}", stem)
    except Exception as e:  # never let debugging break the scrape
        log.warning("Could not save debug snapshot: %s", e)
