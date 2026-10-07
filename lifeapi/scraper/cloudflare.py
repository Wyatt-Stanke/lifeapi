"""Cloudflare's "Just a moment..." challenge page, which some sites (VHL) put in front of any
visitor Cloudflare doesn't trust, such as one from a datacenter IP.

The page runs its checks and then reloads the URL it interrupted, with a `cf_clearance`
cookie that lets later requests through for a while. When the checks aren't conclusive,
Turnstile (the widget in the page) asks for a "Verify you are human" click. `pass_challenge`
waits for the page to clear and makes that click. If the page doesn't clear, it raises
LoginError, so /sources offers `deploy/reauth.py` to finish the challenge by hand. The
cookie that leaves lasts as long as the site's Cloudflare settings allow.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from urllib.parse import urlsplit

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import Locator, Page

from .. import config
from .auth.google import LoginError
from .browser import dump_debug

log = logging.getLogger(__name__)

# Reads only the DOM, so it works from patchright's isolated world. The challenge strips its
# token from the URL as soon as it loads, so the URL can't tell.
CHALLENGE_JS = r"""
() => /^just a moment/i.test(document.title)
  || !!document.querySelector('form#challenge-form')
  || [...document.scripts].some(s => s.textContent.includes('_cf_chl_opt'))
"""
TURNSTILE_FRAME = 'iframe[src*="challenges.cloudflare.com"]'
MAX_CLICKS = 3


async def is_challenge(page: Page) -> bool:
    try:
        return await page.evaluate(CHALLENGE_JS)
    except Exception:  # mid-navigation: the challenge reloading the page, or a redirect
        await page.wait_for_load_state("domcontentloaded")
        return await page.evaluate(CHALLENGE_JS)


async def pass_challenge(page: Page) -> None:
    """If `page` shows a Cloudflare challenge, wait until it clears, clicking Turnstile's
    checkbox when it asks for one. Returns at once if there's no challenge."""
    if not await is_challenge(page):
        return
    host = urlsplit(page.url).hostname
    log.info("Cloudflare challenge on %s; waiting for it to clear", host)
    timeout = config.timeout(30_000)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout / 1000
    checkbox = page.frame_locator(TURNSTILE_FRAME).locator('input[type="checkbox"]')
    # The challenge reloads the page when it passes; the predicate is re-run there.
    cleared = asyncio.ensure_future(page.wait_for_function(f"() => !({CHALLENGE_JS})()", timeout=timeout))
    shown = None
    clicks = 0
    try:
        while True:
            left = max(deadline - loop.time(), 0.1) * 1000
            shown = asyncio.ensure_future(checkbox.wait_for(state="visible", timeout=left))
            done, _ = await asyncio.wait({cleared, shown}, return_when=asyncio.FIRST_COMPLETED)
            if cleared in done and not cleared.exception():
                log.info("Cloudflare challenge on %s cleared%s", host,
                         f" after {clicks} click(s)" if clicks else "")
                return
            if cleared in done or clicks >= MAX_CLICKS:
                break
            if shown.exception():
                if _widget_gone(page, shown.exception()):
                    continue
                break
            clicks += 1
            log.info("Clicking Turnstile's checkbox (%d of %d)", clicks, MAX_CLICKS)
            try:
                await _click(page, checkbox, timeout=max(deadline - loop.time(), 0.1) * 1000)
            except PlaywrightError as e:
                if not _widget_gone(page, e):
                    raise
                continue
            # It turns into a spinner, then the page reloads. If it comes back, it's asking again.
            try:
                await checkbox.wait_for(state="hidden", timeout=config.timeout(10_000))
            except Exception:
                pass
    finally:
        for task in (cleared, shown):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (cleared, shown) if t), return_exceptions=True)
    await dump_debug(page, "cloudflare_challenge")
    raise LoginError(
        f"Cloudflare challenge on {host} didn't clear"
        + (f" after {clicks} click(s) on its checkbox" if clicks else "")
        + ". It's usually the IP (a datacenter's), so it may need a person: finish it by hand, "
        "and the clearance it leaves lasts a while."
    )


def _widget_gone(page: Page, error: BaseException) -> bool:
    """Whether `error` (from waiting for or clicking the checkbox) means only that Turnstile
    replaced its widget. It does that when it resets, and its iframe is out of process, so
    a call in flight on the old one fails with TargetClosedError although the page is fine.
    The caller then looks for the checkbox again."""
    # patchright exports TargetClosedError only from its private _impl package.
    if page.is_closed() or type(error).__name__ != "TargetClosedError":
        return False
    log.info("Turnstile's widget went away mid-call (%s); looking for it again", str(error).splitlines()[0])
    return True


async def _click(page: Page, checkbox: Locator, timeout: float) -> None:
    """Click the checkbox. On an X display (the container's virtual one), as a real pointer
    event through xdotool, which reaches Chrome the way a person's click does. Otherwise,
    and if that fails, with a click sent over CDP."""
    box = await checkbox.bounding_box(timeout=timeout)
    if box and os.environ.get("DISPLAY") and shutil.which("xdotool"):
        # bounding_box() is relative to the top-level viewport; the viewport's top-left
        # on screen is the window's position plus the toolbar above it.
        left, top = await page.evaluate(
            "() => [screenX + (outerWidth - innerWidth) / 2, screenY + outerHeight - innerHeight]")
        x = round(left + box["x"] + box["width"] / 2)
        y = round(top + box["y"] + box["height"] / 2)
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "mousemove", "--sync", str(x), str(y), "click", "1",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode == 0:
            return
        log.warning("xdotool click failed (%s); clicking over CDP", err.decode().strip())
    await checkbox.click(timeout=timeout)
