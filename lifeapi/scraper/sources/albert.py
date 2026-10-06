"""Albert (albert.io) — stub.

There's nothing assigned on Albert yet, so this source is registered but disabled. When
it's needed: sign in at https://www.albert.io/log-in with "Sign in with Google" (reuse
`auth.google.google_login` once the Google popup/redirect appears), then scrape the
student class dashboard's assignments into `Item`s, and flip `enabled` to True.
"""

from __future__ import annotations

from ...models import ScrapeResult
from ..base import Source, register


@register
class Albert(Source):
    name = "albert"
    enabled = False

    async def scrape(self) -> ScrapeResult:
        self.log.info("Albert is a stub; nothing to scrape yet")
        return ScrapeResult()
