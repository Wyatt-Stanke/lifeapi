"""Google account login (also used by "Sign in with Google" buttons on other sites)."""

from __future__ import annotations

import logging

from patchright.async_api import Page

from ... import config
from ..browser import dump_debug, wait_gone, wait_until
from ..trail import redact

log = logging.getLogger(__name__)


class LoginError(RuntimeError):
    pass


def is_google_login_url(url: str) -> bool:
    return "accounts.google.com" in url


def on_google_login(page: Page) -> bool:
    return is_google_login_url(page.url)


async def google_login(page: Page) -> None:
    """Complete a Google sign-in flow on `page`, which must already be on accounts.google.com.

    Handles the account chooser, identifier and password steps, and the occasional
    "Continue"/"Allow"/"Next" interstitial (consent screens, "Verify it's you"). Raises LoginError on anything
    it can't get past unattended (2-step verification, captchas, etc.).
    """
    username = config.credential("GOOGLE_USERNAME")

    for _ in range(8):
        if not on_google_login(page):
            return
        chooser = page.locator(f'[data-identifier="{username}" i]')
        ident = page.locator("#identifierId:visible, input[name=identifier]:visible")
        pw = page.locator('input[name="Passwd"]:visible, input[type="password"]:visible')
        # OAuth consent / "Continue as ..." / "You're signing back in" / "Verify it's you".
        button = (
            page.get_by_role("button", name="Continue")
            .or_(page.get_by_role("button", name="Allow"))
            .or_(page.get_by_role("button", name="I understand"))
            .or_(page.get_by_role("button", name="Next"))
        )
        # The form renders after load, and passive sign-ins redirect away on their own.
        await wait_until(
            page, chooser.or_(ident).or_(pw).or_(button), url=lambda u: not is_google_login_url(u)
        )
        if not on_google_login(page):
            return

        # Account chooser: pick our account if it's listed. Then the identifier, password
        # and interstitial steps, in that order (later screens also have a "Next" button).
        for step in (chooser, ident, pw, button):
            if await step.count():
                break
        else:
            break
        control = await step.first.element_handle()
        if step is chooser or step is button:
            await control.click()
        else:
            secret = username if step is ident else config.credential("GOOGLE_PASSWORD")
            await control.fill(secret)
            await page.keyboard.press("Enter")
        # Steps either navigate or swap content in place; either way the control goes away.
        # If it doesn't (wrong password, a challenge), stop rather than retry it.
        if not await wait_gone(control):
            break

    if on_google_login(page):
        await dump_debug(page, "google_login_stuck")
        raise LoginError(
            f"Stuck on Google sign-in at {redact(page.url)} (2-step verification or a captcha?). "
            "Run `python -m lifeapi.scraper --headed --only google_classroom` once and "
            "finish the sign-in by hand; the session is saved in the browser profile."
        )
