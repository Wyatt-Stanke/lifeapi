"""College Board account login (Okta, at prod.idp.collegeboard.org)."""

from __future__ import annotations

import logging

from patchright.async_api import Page

from ... import config
from ..browser import dump_debug
from .google import LoginError

log = logging.getLogger(__name__)


def on_collegeboard_login(page: Page) -> bool:
    return "idp.collegeboard.org" in page.url or "account.collegeboard.org/login" in page.url


async def collegeboard_login(page: Page) -> None:
    """Complete the identifier -> password Okta flow on `page`."""
    for _ in range(6):
        if not on_collegeboard_login(page):
            return
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)
        url = page.url

        # Cookie banner can cover the form.
        reject = page.locator("#onetrust-reject-all-handler:visible, button:has-text('Reject Optional'):visible")
        if await reject.count():
            await reject.first.click()

        ident = page.locator('input[name="identifier"]:visible')
        pw = page.locator('input[name="credentials.passcode"]:visible, input[type="password"]:visible')
        # "Verify it's you with a security method" chooser: always pick Password (not email).
        choose_pw = page.locator('[data-se="okta_password"] a[data-se="button"]:visible, '
                                 'a[aria-label="Select Password."]:visible')
        # Redirects between account.collegeboard.org and the Okta page take a moment.
        try:
            await ident.or_(pw).or_(choose_pw).first.wait_for(timeout=config.timeout(20_000))
        except Exception:
            if not on_collegeboard_login(page):
                return
            break
        if await choose_pw.count():
            await choose_pw.first.click()
            await page.locator('input[type="password"]:visible').first.wait_for(timeout=config.timeout(10_000))
            continue
        if await pw.count():
            await pw.first.fill(config.credential("COLLEGEBOARD_PASSWORD"))
            await page.keyboard.press("Enter")
        elif await ident.count():
            await ident.first.fill(config.credential("COLLEGEBOARD_USERNAME"))
            await page.keyboard.press("Enter")
        else:
            break
        try:
            await page.wait_for_url(lambda u: u != url, timeout=config.timeout(15_000))
        except Exception:
            pass  # Okta swaps steps in place without a URL change

    if on_collegeboard_login(page):
        await dump_debug(page, "collegeboard_login_stuck")
        raise LoginError(f"Stuck on College Board sign-in at {page.url} (bad password or MFA?)")
