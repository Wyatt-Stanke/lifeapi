"""Scraper entry point.

    python -m lifeapi.scraper                      # run all enabled sources once
    python -m lifeapi.scraper --only infinite_campus
    python -m lifeapi.scraper --headed             # watch it / finish a login by hand
    python -m lifeapi.scraper --list
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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name, cls in sorted(REGISTRY.items()):
            print(f"{name}{'' if cls.enabled else '  (disabled)'}")
        return

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    ok = asyncio.run(run(only=args.only, headless=False if args.headed else None))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
