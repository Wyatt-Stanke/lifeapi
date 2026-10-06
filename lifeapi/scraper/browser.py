"""Stealth browser session built on patchright (an undetected fork of Playwright).

A single persistent profile is shared by every source, so one Google login is reused by
Classroom, Clever, Albert and Infinite Campus, and sessions survive between runs (which
keeps the number of fresh logins — and the chance of security challenges — low).
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from patchright.async_api import BrowserContext, Page, async_playwright

from .. import config

log = logging.getLogger(__name__)


@asynccontextmanager
async def browser_context(
    headless: bool | None = None, profile_dir: Path | None = None
) -> AsyncIterator[BrowserContext]:
    profile_dir = profile_dir or config.BROWSER_PROFILE_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
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
