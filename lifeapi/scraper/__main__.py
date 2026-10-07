"""Scraper entry point.

    python -m lifeapi.scraper                      # run all enabled sources once
    python -m lifeapi.scraper --only infinite_campus
    python -m lifeapi.scraper --headed             # watch it / finish a login by hand
    python -m lifeapi.scraper --list
    python -m lifeapi.scraper --requested          # just what's waiting in POST /sync requests
    python -m lifeapi.scraper --due                # just what's due on its schedule (every minute)
    python -m lifeapi.scraper --only infinite_campus --partial gpa
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import sources  # noqa: F401
from .base import REGISTRY
from .runner import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m lifeapi.scraper")
    parser.add_argument("--only", nargs="+", metavar="SOURCE", help="run just these sources")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--list", action="store_true", help="list available sources")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--requested", action="store_true",
                      help="run only sources with waiting sync requests (POST /sync); "
                           "exits without opening the browser if there are none")
    mode.add_argument("--due", action="store_true",
                      help="run only sources that are due on their schedules (PUT "
                           "/sources/{source}/schedule), in full or as the scheduled partial "
                           "fetch; exits without opening the browser if none are")
    mode.add_argument("--partial", metavar="NAME",
                      help="do this partial fetch instead of a full one (see --list)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name, cls in sorted(REGISTRY.items()):
            notes = ([] if cls.enabled else ["disabled"]) + [f"partial {p}: {d}" for p, d in cls.partials.items()]
            print(name + "".join(f"  ({n})" for n in notes))
        return

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # lifeapi always logs debug lines, for the trail saved with a failed run (trail.py);
    # the console shows them only with -v.
    logging.getLogger().handlers[0].setLevel(level)
    logging.getLogger("lifeapi").setLevel(logging.DEBUG)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    ok = asyncio.run(run(only=args.only, headless=False if args.headed else None,
                         requested=args.requested, due=args.due, partial=args.partial))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
