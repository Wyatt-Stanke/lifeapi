"""Runs every enabled source once, storing results. Each source is isolated: one failing
(bad selector, login challenge) doesn't stop the others, and its old data is kept.

A run also settles the manual sync requests (`POST /sync`) it covers. `requested=True`
runs exactly the sources that are waiting in requests, and nothing if none are."""

from __future__ import annotations

import fcntl
import logging
import traceback
from contextlib import contextmanager
from typing import Iterator

from .. import config, storage
from ..models import Item
from . import sources  # noqa: F401  (registers all sources)
from .base import REGISTRY, Source
from .browser import browser_context
from .trail import Trail

log = logging.getLogger("lifeapi.scraper")


def _previous_items(conn, source: str) -> dict[str, Item]:
    rows = conn.execute("SELECT data FROM items WHERE source=? AND active=1", (source,))
    return {i.id: i for i in (Item.model_validate_json(r["data"]) for r in rows)}


@contextmanager
def _run_lock() -> Iterator[None]:
    """One run at a time: a second one waits here instead of failing on Chrome's profile lock."""
    config.RUN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(config.RUN_LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Waiting for another scraper run to finish")
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _expand(sources: list[str] | None) -> set[str]:
    """A request's sources; None means every enabled one."""
    return set(sources) if sources is not None else {n for n, c in REGISTRY.items() if c.enabled}


async def run(only: list[str] | None = None, headless: bool | None = None,
              requested: bool = False) -> bool:
    """Scrape the selected sources. Returns True if all of them succeeded."""
    if only:
        unknown = set(only) - REGISTRY.keys()
        if unknown:
            raise SystemExit(f"Unknown source(s): {', '.join(sorted(unknown))}. "
                             f"Available: {', '.join(sorted(REGISTRY))}")

    with _run_lock(), storage.connect() as conn:
        storage.abandon_sync_requests(conn)
        # Remove the trigger before reading requests: one queued after this point either
        # gets claimed below or re-creates the trigger, so none is missed.
        config.SYNC_TRIGGER.unlink(missing_ok=True)
        pending = {i: _expand(s) for i, s in storage.pending_sync_requests(conn).items()}
        for i, wanted in list(pending.items()):
            if unknown := wanted - REGISTRY.keys():  # a source removed since it was queued
                storage.start_sync_requests(conn, [i])
                storage.finish_sync_request(conn, i, f"Unknown source(s): {', '.join(sorted(unknown))}")
                del pending[i]

        if requested:
            names = set().union(*pending.values())
            if only:
                names &= set(only)
            if not names:
                log.info("No sync requests waiting")
                if pending:  # all for sources outside --only
                    config.SYNC_TRIGGER.touch()
                return True
        else:
            names = set(only) if only else _expand(None)
        selected = [cls for n, cls in REGISTRY.items() if n in names]

        claimed = {i: wanted for i, wanted in pending.items() if wanted <= names}
        storage.start_sync_requests(conn, list(claimed))
        if len(claimed) < len(pending):
            config.SYNC_TRIGGER.touch()  # the rest wait for another run
        if claimed:
            log.info("Covering sync request(s) %s", ", ".join(map(str, claimed)))

        errors: dict[str, str] = {}
        try:
            async with browser_context(headless=headless) as ctx:
                for cls in selected:
                    if error := await _run_one(conn, ctx, cls):
                        errors[cls.name] = error
        except Exception as e:
            for i in claimed:
                storage.finish_sync_request(conn, i, f"{type(e).__name__}: {e}")
            raise
        for i, wanted in claimed.items():
            failed = sorted(wanted & errors.keys())
            storage.finish_sync_request(
                conn, i, "; ".join(f"{n}: {errors[n]}" for n in failed) if failed else None)
    return not errors


def describe_error(e: BaseException, last_url: str | None = None) -> str:
    """What went wrong, for `scrape_runs.error`: the exception, the steps it happened in
    (`Source.step` notes), what caused it, and the last page a tab was on. Starts with
    "<ExceptionName>: " (the API looks for "LoginError:")."""
    lines = [f"{type(e).__name__}: {e}".strip()]
    lines += [f"  {note}" for note in getattr(e, "__notes__", ())]
    cause = e.__cause__ or (None if e.__suppress_context__ else e.__context__)
    seen = {id(e)}
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        lines.append(f"caused by {type(cause).__name__}: {cause}".strip())
        lines += [f"  {note}" for note in getattr(cause, "__notes__", ())]
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    if last_url:
        lines.append(f"last page: {last_url}")
    return "\n".join(lines)


async def _run_one(conn, ctx, cls: type[Source]) -> str | None:
    """Scrape one source and store the result. Returns the error, or None on success.
    A failed run also stores its trail (see `trail.py`) for piecing together what happened."""
    log.info("Scraping %s", cls.name)
    run_id = storage.start_run(conn, cls.name)
    with Trail(ctx) as trail:
        try:
            source = cls(ctx, previous=_previous_items(conn, cls.name))
            result = await source.scrape()
        except Exception as e:
            error = describe_error(e, trail.last_url)
            tb = traceback.format_exc()
            run_log = f"{cls.name} run {run_id}\n{error}\n\n--- trail ---\n{trail.text()}\n\n--- traceback ---\n{tb}"
            log.error("%s failed: %s", cls.name, error)
            log.debug("%s", tb)
            storage.finish_run(conn, run_id, None, error=error, log=run_log)
            return error
    storage.save_result(conn, cls.name, result)
    storage.finish_run(conn, run_id, result)
    log.info("%s: %d courses, %d items, %d grades",
             cls.name, len(result.courses), len(result.items), len(result.grades))
    return None
