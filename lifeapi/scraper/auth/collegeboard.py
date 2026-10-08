"""College Board account login (Okta, at prod.idp.collegeboard.org)."""

from __future__ import annotations

import logging

from patchright.async_api import Page

from ... import config
from ..browser import dump_debug, wait_gone, wait_until
from ..trail import redact
from .google import LoginError

log = logging.getLogger(__name__)


def is_collegeboard_login_url(url: str) -> bool:
    return "idp.collegeboard.org" in url or "account.collegeboard.org/login" in url


def is_myap_login_url(url: str) -> bool:
    """MyAP's own sign-in page: a Student/Educator chooser that doesn't redirect by itself.
    Apps that log the user out sometimes send them there instead of the Okta sign-in."""
    return "myap.collegeboard.org/login" in url


def is_collegeboard_error_url(url: str) -> bool:
    """College Board's generic sign-in failure page, which it shows when the last step
    (trading Okta's code for a College Board session) fails."""
    return "account.collegeboard.org/login/error" in url


def on_collegeboard_login(page: Page) -> bool:
    return is_collegeboard_login_url(page.url)


async def collegeboard_login(page: Page) -> None:
    """Complete the identifier -> password Okta flow on `page`."""
    for _ in range(6):
        if not on_collegeboard_login(page):
            return
        await page.wait_for_load_state("domcontentloaded")

        ident = page.locator('input[name="identifier"]:visible')
        pw = page.locator('input[name="credentials.passcode"]:visible, input[type="password"]:visible')
        # "Verify it's you with a security method" chooser: always pick Password (not email).
        choose_pw = page.locator('[data-se="okta_password"] a[data-se="button"]:visible, '
                                 'a[aria-label="Select Password."]:visible')
        # Redirects between account.collegeboard.org and the Okta page take a moment. After
        # the password, the flow leaves through account.collegeboard.org/login/exchangeToken,
        # which has no form, so also stop waiting once the page leaves the sign-in pages.
        found = await wait_until(
            page, ident.or_(pw).or_(choose_pw),
            url=lambda u: not is_collegeboard_login_url(u) or is_collegeboard_error_url(u),
            timeout=config.timeout(20_000),
        )
        if not on_collegeboard_login(page):
            return
        if not found or is_collegeboard_error_url(page.url):
            break

        # Cookie banner can cover the form.
        reject = page.locator("#onetrust-reject-all-handler:visible, button:has-text('Reject Optional'):visible")
        if await reject.count():
            await reject.first.click()

        if await choose_pw.count():
            await choose_pw.first.click()
            await page.locator('input[type="password"]:visible').first.wait_for(timeout=config.timeout(10_000))
            continue
        if await pw.count():
            field, value = pw, config.credential("COLLEGEBOARD_PASSWORD")
        elif await ident.count():
            field, value = ident, config.credential("COLLEGEBOARD_USERNAME")
        else:
            break
        control = await field.first.element_handle()
        await control.fill(value)
        await page.keyboard.press("Enter")
        # Okta either navigates or swaps the step in place. If the field stays (bad
        # password, MFA prompt), stop rather than submit it again.
        if not await wait_gone(control):
            break

    if on_collegeboard_login(page):
        await dump_debug(page, "collegeboard_login_stuck")
        if is_collegeboard_error_url(page.url):
            raise LoginError(
                f"College Board's sign-in ended on its error page ({redact(page.url)}) after the "
                "password was accepted, while trading the sign-in code for a session")
        raise LoginError(f"Stuck on College Board sign-in at {redact(page.url)} (bad password or MFA?)")
