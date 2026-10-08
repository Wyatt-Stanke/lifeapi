"""Clever SSO: open the district portal, sign in with Google, and launch an app from the
dashboard (which signs into that app automatically)."""

from __future__ import annotations

import logging
import re

from patchright.async_api import BrowserContext, Page

from ... import config
from ..browser import dump_debug, wait_until
from ..trail import redact
from .google import LoginError, google_login, is_google_login_url, on_google_login

log = logging.getLogger(__name__)


async def clever_dashboard(page: Page) -> None:
    """Get `page` onto the signed-in Clever student dashboard."""
    response = await page.goto(config.CLEVER_PORTAL_URL)
    # Clever's portal sometimes answers 503 from its load balancer. The page then never
    # shows a sign-in button or apps, so say so now rather than time out looking for them.
    if response and response.status >= 500:
        raise RuntimeError(f"Clever is down: HTTP {response.status} for {redact(page.url)}. Try again later.")
    google_btn = page.get_by_role("link", name=re.compile("google", re.I)).or_(
        page.get_by_role("button", name=re.compile("google", re.I))
    )
    apps = page.get_by_text(re.compile("My apps|Apps", re.I))
    for _ in range(4):
        await page.wait_for_load_state("domcontentloaded")
        # Wait for the page to show one of the states handled below.
        await wait_until(page, google_btn.or_(apps), url=lambda u: is_google_login_url(u) or _on_dashboard(u))
        if on_google_login(page):
            await google_login(page)
            continue
        if _on_dashboard(page.url):
            return
        if await google_btn.count():
            url = page.url
            await google_btn.first.click()
            try:
                await page.wait_for_url(lambda u: u != url, timeout=config.timeout(15_000))
            except Exception:
                pass
            continue
        if "clever.com" in page.url and await apps.count():
            return
    if not re.search(r"clever\.com", page.url):
        await dump_debug(page, "clever_stuck")
        raise LoginError(f"Could not reach the Clever dashboard (ended at {page.url})")


def _on_dashboard(url: str) -> bool:
    return bool(re.search(r"clever\.com/(in/)?[^/]*/?student|/applications|clever\.com/home", url))


async def launch_app(context: BrowserContext, page: Page, app_name: str) -> Page:
    """Click an app tile on the Clever dashboard; returns the page the app opened in."""
    await clever_dashboard(page)
    tile = page.get_by_role("link", name=re.compile(re.escape(app_name), re.I)).or_(
        page.get_by_role("button", name=re.compile(re.escape(app_name), re.I))
    )
    try:
        await tile.first.wait_for(timeout=config.timeout(20_000))
    except Exception as e:
        await dump_debug(page, "clever_no_app")
        raise LoginError(f"No {app_name!r} app on the Clever dashboard") from e
    # Tiles usually open a new tab.
    try:
        async with context.expect_page(timeout=config.timeout(10_000)) as new:
            await tile.first.click()
        app_page = await new.value
    except Exception:
        app_page = page
    await app_page.wait_for_load_state("domcontentloaded")
    return app_page
