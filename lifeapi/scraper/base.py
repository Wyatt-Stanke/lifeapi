"""Base class and registry for sources.

To add a new platform:
  1. Create `lifeapi/scraper/sources/<name>.py`.
  2. Subclass `Source`, set `name`, implement `scrape()`.
  3. Decorate it with `@register`, and import the module in `sources/__init__.py`.
Login helpers for shared identity providers live in `lifeapi/scraper/auth/`.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, ClassVar, Iterable, TypeVar

from patchright.async_api import BrowserContext, Page

from ..models import Item, ScrapeResult

REGISTRY: dict[str, type["Source"]] = {}

T = TypeVar("T")
R = TypeVar("R")


def register(cls: type["Source"]) -> type["Source"]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} must set `name`")
    REGISTRY[cls.name] = cls
    return cls


class Source(ABC):
    name: ClassVar[str] = ""
    # Disabled sources are skipped unless requested explicitly with --only.
    enabled: ClassVar[bool] = True

    def __init__(self, context: BrowserContext, previous: dict[str, Item] | None = None):
        self.context = context
        # Items this source produced on earlier runs, keyed by id. Lets a source skip
        # re-fetching detail pages for things that haven't changed.
        self.previous = previous or {}
        self.log = logging.getLogger(f"lifeapi.source.{self.name}")

    @abstractmethod
    async def scrape(self) -> ScrapeResult:
        """Log in if needed and return everything this source currently shows."""

    async def new_page(self) -> Page:
        return await self.context.new_page()

    async def map_pages(
        self,
        items: Iterable[T],
        fn: Callable[[Page, T], Awaitable[R]],
        concurrency: int = 3,
    ) -> list[R | BaseException]:
        """Run `fn(page, item)` for each item over a small pool of tabs.

        Exceptions are returned in place of results so one bad page doesn't sink the run.
        """
        queue: asyncio.Queue[tuple[int, T]] = asyncio.Queue()
        items = list(items)
        for pair in enumerate(items):
            queue.put_nowait(pair)
        results: list[R | BaseException] = [None] * len(items)  # type: ignore[list-item]

        async def worker() -> None:
            page = await self.new_page()
            try:
                while not queue.empty():
                    i, item = queue.get_nowait()
                    try:
                        results[i] = await fn(page, item)
                    except Exception as e:
                        self.log.warning("Failed on %r: %s", item, e)
                        results[i] = e
            finally:
                await page.close()

        await asyncio.gather(*(worker() for _ in range(min(concurrency, len(items)) or 0)))
        return results
