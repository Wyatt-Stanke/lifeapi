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
# Multiplies every scraper wait deadline (page loads, selectors, logins). Small hosts render
# slowly, and a probe that gives up early can read as "no items" and soft-delete real data.
TIMEOUT_SCALE = float(os.getenv("LIFEAPI_TIMEOUT_SCALE", "3"))


def timeout(ms: int) -> int:
    """A wait deadline in ms, scaled by LIFEAPI_TIMEOUT_SCALE."""
    return int(ms * TIMEOUT_SCALE)

# The longest one source's scrape may take, in seconds (not scaled by TIMEOUT_SCALE). A run
# that's still going after this is stopped and recorded as failed, with its trail, so one
# hung page can't hold up every other source. A full Google Classroom re-read, the slowest,
# has taken about 16 minutes on the server.
SOURCE_TIMEOUT = int(os.getenv("LIFEAPI_SOURCE_TIMEOUT", "1800"))

# How often a source is fetched (in full) when no schedule has been set for it through the
# API (`PUT /sources/{source}/schedule`). The variable is in seconds. The scraper's `--due`
# runs follow it, and the API reports it, so both need the same value.
SCRAPE_INTERVAL_MINUTES = max(1, round(int(os.getenv("LIFEAPI_SCRAPE_INTERVAL", "7200")) / 60))

# An upstream proxy (http://user:password@host:port) for sites whose bot checks distrust
# this host's IP: VHL's Cloudflare challenges a datacenter's. Only the hosts in
# PROXY_DOMAINS, and their subdomains, go through it (see scraper/proxy.py).
PROXY = os.getenv("LIFEAPI_PROXY") or None
PROXY_DOMAINS = [d.strip().lstrip(".") for d in
                 (os.getenv("LIFEAPI_PROXY_DOMAINS") or "vhlcentral.com,challenges.cloudflare.com").split(",")
                 if d.strip()]

# Optional bearer token for the API. If unset, the API is open (bind it to localhost).
API_TOKEN = os.getenv("LIFEAPI_API_TOKEN") or None

# Where `deploy/reauth.py` should connect when a sign-in needs finishing by hand: the
# server's SSH destination (e.g. "root@vps"), or "--local" for containers on this machine.
# Only used to show the command in /sources (behind the token), never to connect.
REAUTH_TARGET = os.getenv("LIFEAPI_REAUTH_TARGET") or None


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
