"""Stealth browser session built on patchright (an undetected fork of Playwright).

A single persistent profile is shared by every source, so one Google login is reused by
Classroom, Clever, Albert and Infinite Campus, and sessions survive between runs (which
keeps the number of fresh logins — and the chance of security challenges — low).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import AsyncIterator, Callable

from patchright.async_api import BrowserContext, ElementHandle, Locator, Page, async_playwright
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from .. import config

log = logging.getLogger(__name__)


@asynccontextmanager
async def browser_context(
    headless: bool | None = None, profile_dir: Path | None = None
) -> AsyncIterator[BrowserContext]:
    profile_dir = profile_dir or config.BROWSER_PROFILE_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = config.HEADLESS if headless is None else headless
    display = virtual_display() if not headless and _no_display() else nullcontext()
    async with display, async_playwright() as pw:
        # patchright's recommended stealth setup: real Chrome, persistent profile,
        # no custom viewport/user agent (those are fingerprintable).
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=config.BROWSER_CHANNEL,
            headless=headless,
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
        ctx.set_default_timeout(config.timeout(30_000))
        try:
            yield ctx
        finally:
            await ctx.close()


def _no_display() -> bool:
    """A Linux host with no display to show a headed browser on, like the container."""
    return sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@asynccontextmanager
async def virtual_display() -> AsyncIterator[str]:
    """Run an Xvfb display and point DISPLAY at it while it runs, for a headed browser on a
    host without a screen (and for xdotool's clicks in cloudflare.py). Yields its name."""
    read_fd, write_fd = os.pipe()
    proc = None
    try:
        # -displayfd: Xvfb picks a free display number and writes it once it's accepting
        # connections, so there's nothing to poll for.
        proc = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
            pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(write_fd)
        write_fd = None
        number = await asyncio.wait_for(asyncio.to_thread(os.read, read_fd, 16),
                                        config.timeout(10_000) / 1000)
        if not number.strip():
            raise RuntimeError(f"Xvfb exited with status {proc.wait()} before starting a display")
        name = f":{number.decode().strip()}"
        os.environ["DISPLAY"] = name
        log.debug("Started a virtual display (Xvfb) on %s", name)
        yield name
    finally:
        os.environ.pop("DISPLAY", None)
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # After Xvfb has exited, so a read still waiting in its thread gets end-of-file.
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


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


async def wait_until(
    page: Page,
    shown: Locator | None = None,
    url: Callable[[str], bool] | None = None,
    timeout: int | None = None,
) -> bool:
    """Wait until `shown` is visible or the page URL satisfies `url`, whichever comes first.

    Returns False on timeout instead of raising, for multi-step flows where the caller
    then decides what the page is showing.
    """
    timeout = timeout if timeout is not None else config.timeout(15_000)
    waits = []
    if shown is not None:
        waits.append(asyncio.ensure_future(shown.first.wait_for(timeout=timeout)))
    if url is not None:
        waits.append(asyncio.ensure_future(page.wait_for_url(url, timeout=timeout)))
    pending = set(waits)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(not t.exception() for t in done):
                return True
        return False
    finally:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def wait_gone(control: ElementHandle) -> bool:
    """Wait for a control we just submitted to go away (hidden, swapped out, or navigated
    away from). False if it's still there, e.g. a rejected password."""
    try:
        await control.wait_for_element_state("hidden", timeout=config.timeout(15_000))
    except PlaywrightTimeoutError:
        return False
    except Exception:
        pass  # the page navigated away underneath the handle
    return True
