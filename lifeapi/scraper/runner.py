"""Runs every enabled source once, storing results. Each source is isolated: one failing
(bad selector, login challenge) doesn't stop the others, and its old data is kept."""

from __future__ import annotations

import logging
import traceback

from .. import storage
from ..models import Item
from . import sources  # noqa: F401  (registers all sources)
from .base import REGISTRY, Source
from .browser import browser_context

log = logging.getLogger("lifeapi.scraper")


def _previous_items(conn, source: str) -> dict[str, Item]:
    rows = conn.execute("SELECT data FROM items WHERE source=? AND active=1", (source,))
    return {i.id: i for i in (Item.model_validate_json(r["data"]) for r in rows)}


async def run(only: list[str] | None = None, headless: bool | None = None) -> bool:
    """Scrape the selected sources. Returns True if all of them succeeded."""
    if only:
        unknown = set(only) - REGISTRY.keys()
        if unknown:
            raise SystemExit(f"Unknown source(s): {', '.join(sorted(unknown))}. "
                             f"Available: {', '.join(sorted(REGISTRY))}")
        selected = [REGISTRY[n] for n in only]
    else:
        selected = [cls for cls in REGISTRY.values() if cls.enabled]

    all_ok = True
    with storage.connect() as conn:
        async with browser_context(headless=headless) as ctx:
            for cls in selected:
                all_ok &= await _run_one(conn, ctx, cls)
    return all_ok


async def _run_one(conn, ctx, cls: type[Source]) -> bool:
    log.info("Scraping %s", cls.name)
    run_id = storage.start_run(conn, cls.name)
    try:
        source = cls(ctx, previous=_previous_items(conn, cls.name))
        result = await source.scrape()
    except Exception as e:
        log.error("%s failed: %s", cls.name, e)
        log.debug("%s", traceback.format_exc())
        storage.finish_run(conn, run_id, None, error=f"{type(e).__name__}: {e}")
        return False
    storage.save_result(conn, cls.name, result)
    storage.finish_run(conn, run_id, result)
    log.info("%s: %d courses, %d items, %d grades",
             cls.name, len(result.courses), len(result.items), len(result.grades))
    return True
