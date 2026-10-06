"""Google account login (also used by "Sign in with Google" buttons on other sites)."""

from __future__ import annotations

import logging

from patchright.async_api import Page

from ... import config
from ..browser import dump_debug

log = logging.getLogger(__name__)


class LoginError(RuntimeError):
    pass


def on_google_login(page: Page) -> bool:
    return "accounts.google.com" in page.url


async def google_login(page: Page) -> None:
    """Complete a Google sign-in flow on `page`, which must already be on accounts.google.com.

    Handles the account chooser, identifier and password steps, and the occasional
    "Continue"/"Allow" consent screen for third-party apps. Raises LoginError on anything
    it can't get past unattended (2-step verification, captchas, etc.).
    """
    username = config.credential("GOOGLE_USERNAME")

    for _ in range(8):
        if not on_google_login(page):
            return
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)
        url = page.url

        # Account chooser: pick our account if it's listed.
        chooser = page.locator(f'[data-identifier="{username}" i]')
        if await chooser.count():
            await chooser.first.click()
            await _wait_for_change(page, url)
            continue

        ident = page.locator("#identifierId:visible, input[name=identifier]:visible")
        if await ident.count():
            await ident.first.fill(username)
            await page.keyboard.press("Enter")
            await _wait_for_change(page, url)
            continue

        pw = page.locator('input[name="Passwd"]:visible, input[type="password"]:visible')
        if await pw.count():
            await pw.first.fill(config.credential("GOOGLE_PASSWORD"))
            await page.keyboard.press("Enter")
            await _wait_for_change(page, url)
            continue

        # OAuth consent / "Continue as ..." / "You're signing back in" interstitials.
        button = page.get_by_role("button", name="Continue").or_(
            page.get_by_role("button", name="Allow")
        ).or_(page.get_by_role("button", name="I understand"))
        if await button.count():
            await button.first.click()
            await _wait_for_change(page, url)
            continue

        break

    if on_google_login(page):
        await dump_debug(page, "google_login_stuck")
        raise LoginError(
            f"Stuck on Google sign-in at {page.url} (2-step verification or a captcha?). "
            "Run `python -m lifeapi.scraper --headed --only google_classroom` once and "
            "finish the sign-in by hand; the session is saved in the browser profile."
        )


async def _wait_for_change(page: Page, old_url: str, timeout_ms: int = 15_000) -> None:
    try:
        await page.wait_for_url(lambda u: u != old_url, timeout=config.timeout(timeout_ms))
    except Exception:
        pass  # some steps change content without changing the URL
    await page.wait_for_load_state("domcontentloaded")
