"""Runtime configuration, read from the environment (and `.env` at the project root)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = Path(os.getenv("LIFEAPI_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = Path(os.getenv("LIFEAPI_DB_PATH", DATA_DIR / "lifeapi.db"))
BROWSER_PROFILE_DIR = Path(os.getenv("LIFEAPI_BROWSER_PROFILE", DATA_DIR / "browser-profile"))
DEBUG_DIR = Path(os.getenv("LIFEAPI_DEBUG_DIR", DATA_DIR / "debug"))
# The API touches this file when someone asks for a sync; the launchd sync job watches it
# and the container's scrape loop polls it. The scraper removes it when it picks requests up.
SYNC_TRIGGER = DATA_DIR / "sync-requested"
# Held for the whole of a scraper run, so runs queue up instead of fighting over the
# browser profile.
RUN_LOCK = DATA_DIR / "run.lock"

# "chrome" uses the locally installed Google Chrome (best stealth); "" falls back to
# patchright's bundled Chromium.
BROWSER_CHANNEL = os.getenv("LIFEAPI_BROWSER_CHANNEL", "chrome") or None
HEADLESS = os.getenv("LIFEAPI_HEADLESS", "1") not in ("0", "false", "no")

# Optional bearer token for the API. If unset, the API is open (bind it to localhost).
API_TOKEN = os.getenv("LIFEAPI_API_TOKEN") or None


def credential(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing credential {name!r} (set it in .env)")
    return value


# Source-specific URLs live here so they're easy to change without touching scraper code.
CLEVER_PORTAL_URL = os.getenv("CLEVER_PORTAL_URL", "https://clever.com/in/jerseycity/")
INFINITE_CAMPUS_URL = os.getenv(
    "INFINITE_CAMPUS_URL",
    "https://jerseycitynj.infinitecampus.org/campus/portal/students/jerseycity.jsp",
)
