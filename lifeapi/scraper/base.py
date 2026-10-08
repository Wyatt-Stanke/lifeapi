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
from contextlib import contextmanager
from datetime import datetime
from typing import Awaitable, Callable, ClassVar, Iterable, Iterator, TypeVar

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


class SiteUnavailable(RuntimeError):
    """The site failed, not the scraper: it answered with an HTTP 5xx, say. There's nothing
    to fix, and the next run tries again. The run is recorded with `failure="site"`."""


class Source(ABC):
    name: ClassVar[str] = ""
    # Disabled sources are skipped unless requested explicitly with --only.
    enabled: ClassVar[bool] = True
    # Partial fetches a schedule can run between full ones: name -> what it fetches (shown
    # in the API and the explorer). `scrape()` checks `self.partial`. A partial result only
    # adds and updates records; anything it doesn't return stays as it was.
    partials: ClassVar[dict[str, str]] = {}
    # Run in a headed (visible) browser by default rather than a headless one. Bot checks
    # spot headless Chrome more easily. The API (`PUT /sources/{source}/browser`) overrides
    # it per source; the runner launches headed sources in a browser of their own.
    headed: ClassVar[bool] = False

    def __init__(self, context: BrowserContext, previous: dict[str, Item] | None = None,
                 partial: str | None = None, last_run_at: datetime | None = None):
        self.context = context
        # Items this source produced on earlier runs, keyed by id. Lets a source skip
        # re-fetching detail pages for things that haven't changed.
        self.previous = previous or {}
        # One of `partials` for a partial fetch, None for a full one.
        self.partial = partial
        # When the last successful full run started (None if there hasn't been one), so a
        # source can pace periodic re-reads to however often it actually runs.
        self.last_run_at = last_run_at
        self.log = logging.getLogger(f"lifeapi.source.{self.name}")
        # Steps in progress, outermost first (tasks running in parallel interleave), so a run
        # stopped from outside (the runner's time limit) can still say what it was doing.
        self.active_steps: list[str] = []

    @abstractmethod
    async def scrape(self) -> ScrapeResult:
        """Log in if needed and return everything this source currently shows."""

    @contextmanager
    def step(self, what: str) -> Iterator[None]:
        """Mark a phase of the scrape, e.g. `with self.step(f"reading {course.name}"):`.
        An exception escaping it gets a "while <what>" note, so a bare Playwright timeout
        says what it was in the middle of. Steps nest; the innermost note comes first."""
        self.log.debug("Step: %s", what)
        self.active_steps.append(what)
        try:
            yield
        except Exception as e:
            e.add_note(f"while {what}")
            raise
        finally:
            self.active_steps.remove(what)

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
