"""Runs every enabled source once, storing results. Each source is isolated: one failing
(bad selector, login challenge) doesn't stop the others, and its old data is kept.

A run also settles the manual sync requests (`POST /sync`) whose sources it fetches in full.
`requested=True` runs exactly the sources that are waiting in requests, and nothing if none
are. `due=True` runs the sources their schedules (`PUT /sources/{source}/schedule`) say are
due, each in full or as the scheduled partial fetch, and nothing if none are.

Sources run headless unless set to headed (`Source.headed`, or `PUT
/sources/{source}/browser`). Headed ones get a browser launch of their own, after the
headless ones; both use the same profile, so sign-ins carry over."""

from __future__ import annotations

import fcntl
import logging
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
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


def _headless(conn, cls: type[Source], forced: bool | None) -> bool:
    """Whether `cls` runs headless: `forced` (`--headed`) if set, headed for every source
    with LIFEAPI_HEADLESS=0, else the source's own setting."""
    if forced is not None:
        return forced
    if not config.HEADLESS:
        return False
    return not storage.get_browser(conn, cls.name, cls.headed)["headed"]


def _due(conn) -> dict[str, str | None]:
    """Enabled sources their schedules say are due now, each with the partial fetch to run
    (None: a full one)."""
    now = datetime.now(timezone.utc)
    plan = {}
    for name, cls in REGISTRY.items():
        if cls.enabled:
            at, partial = storage.next_fetch(conn, name, storage.get_schedule(conn, name, cls.partials))
            if at is None or at <= now:
                plan[name] = partial
    return plan


async def run(only: list[str] | None = None, headless: bool | None = None,
              requested: bool = False, due: bool = False, partial: str | None = None) -> bool:
    """Scrape the selected sources, in full or (`partial`, or as scheduled with `due`) as a
    partial fetch. `headless` forces every source headless or headed; None follows each
    source's setting. Returns True if all of them succeeded."""
    if only:
        unknown = set(only) - REGISTRY.keys()
        if unknown:
            raise SystemExit(f"Unknown source(s): {', '.join(sorted(unknown))}. "
                             f"Available: {', '.join(sorted(REGISTRY))}")
    if partial:
        if lacking := sorted(n for n in only or _expand(None) if partial not in REGISTRY[n].partials):
            raise SystemExit(f"No partial fetch {partial!r} in {', '.join(lacking)} (see --list)")

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

        # Source name -> the partial fetch to run, None for a full one.
        plan: dict[str, str | None]
        if requested:
            plan = dict.fromkeys(set().union(*pending.values()))
        elif due:
            plan = _due(conn)
        else:
            plan = dict.fromkeys(set(only) if only else _expand(None), partial)
        if only:
            plan = {n: p for n, p in plan.items() if n in only}
        selected = [(cls, plan[n]) for n, cls in REGISTRY.items() if n in plan]

        # A request is settled by a run that fetches all of its sources in full.
        full = {n for n, p in plan.items() if p is None}
        claimed = {i: wanted for i, wanted in pending.items() if wanted <= full}
        if len(claimed) < len(pending):
            config.SYNC_TRIGGER.touch()  # the rest wait for another run
        if not selected:
            if requested:
                log.info("No sync requests waiting")
            else:
                log.debug("No sources due")  # debug: --due runs every minute
            return True
        storage.start_sync_requests(conn, list(claimed))
        if claimed:
            log.info("Covering sync request(s) %s", ", ".join(map(str, claimed)))

        # Headless sources first, then headed ones, each group in one browser launch.
        groups: dict[bool, list[tuple[type[Source], str | None]]] = {True: [], False: []}
        for cls, part in selected:
            groups[_headless(conn, cls, headless)].append((cls, part))
        errors: dict[str, str] = {}
        try:
            for mode, group in groups.items():
                if not group:
                    continue
                async with browser_context(headless=mode) as ctx:
                    for cls, part in group:
                        if error := await _run_one(conn, ctx, cls, part):
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


async def _run_one(conn, ctx, cls: type[Source], partial: str | None = None) -> str | None:
    """Scrape one source (`partial`: just that partial fetch) and store the result. Returns
    the error, or None on success. A failed run also stores its trail (see `trail.py`) for
    piecing together what happened."""
    log.info("Scraping %s%s", cls.name, f" (partial: {partial})" if partial else "")
    run_id = storage.start_run(conn, cls.name, partial)
    with Trail(ctx) as trail:
        try:
            source = cls(ctx, previous=_previous_items(conn, cls.name), partial=partial,
                         last_run_at=storage.last_full_run(conn, cls.name))
            result = await source.scrape()
        except Exception as e:
            error = describe_error(e, trail.last_url)
            tb = traceback.format_exc()
            run_log = f"{cls.name} run {run_id}\n{error}\n\n--- trail ---\n{trail.text()}\n\n--- traceback ---\n{tb}"
            log.error("%s failed: %s", cls.name, error)
            log.debug("%s", tb)
            storage.finish_run(conn, run_id, None, error=error, log=run_log)
            return error
    storage.save_result(conn, cls.name, result, partial=partial is not None)
    storage.finish_run(conn, run_id, result)
    log.info("%s: %d courses, %d items, %d grades",
             cls.name, len(result.courses), len(result.items), len(result.grades))
    return None
