"""Clever SSO: open the district portal, sign in with Google, and launch an app from the
dashboard (which signs into that app automatically)."""

from __future__ import annotations

import logging
import re

from patchright.async_api import BrowserContext, Page

from ... import config
from ..browser import dump_debug
from .google import LoginError, google_login, on_google_login

log = logging.getLogger(__name__)


async def clever_dashboard(page: Page) -> None:
    """Get `page` onto the signed-in Clever student dashboard."""
    await page.goto(config.CLEVER_PORTAL_URL)
    for _ in range(4):
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)
        if on_google_login(page):
            await google_login(page)
            continue
        if re.search(r"clever\.com/(in/)?[^/]*/?student|/applications|clever\.com/home", page.url):
            return
        google_btn = page.get_by_role("link", name=re.compile("google", re.I)).or_(
            page.get_by_role("button", name=re.compile("google", re.I))
        )
        if await google_btn.count():
            await google_btn.first.click()
            continue
        if "clever.com" in page.url and await page.get_by_text(re.compile("My apps|Apps", re.I)).count():
            return
    if not re.search(r"clever\.com", page.url):
        await dump_debug(page, "clever_stuck")
        raise LoginError(f"Could not reach the Clever dashboard (ended at {page.url})")


async def launch_app(context: BrowserContext, page: Page, app_name: str) -> Page:
    """Click an app tile on the Clever dashboard; returns the page the app opened in."""
    await clever_dashboard(page)
    tile = page.get_by_role("link", name=re.compile(re.escape(app_name), re.I)).or_(
        page.get_by_role("button", name=re.compile(re.escape(app_name), re.I))
    )
    try:
        await tile.first.wait_for(timeout=20_000)
    except Exception as e:
        await dump_debug(page, "clever_no_app")
        raise LoginError(f"No {app_name!r} app on the Clever dashboard") from e
    # Tiles usually open a new tab.
    try:
        async with context.expect_page(timeout=10_000) as new:
            await tile.first.click()
        app_page = await new.value
    except Exception:
        app_page = page
    await app_page.wait_for_load_state("domcontentloaded")
    return app_page
