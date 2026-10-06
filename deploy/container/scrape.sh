#!/bin/sh
# One scraper run, holding the profile lock so it never overlaps another run (the loop,
# or login.sh started with `exec`). Arguments go to `python -m lifeapi.scraper`.
set -e
DATA="${LIFEAPI_DATA_DIR:-/data}"
PROFILE="${LIFEAPI_BROWSER_PROFILE:-$DATA/browser-profile}"
mkdir -p "$PROFILE"

exec 9>"$DATA/scrape.lock"
flock -n 9 || { echo "scrape.sh: waiting for the current run to finish"; flock 9; }
# Chrome's Singleton* files name the host that held the profile. A recreated container
# has a new hostname, and Chrome then refuses the profile as "in use on another computer".
# We hold the lock, so nothing else is using it.
rm -f "$PROFILE"/Singleton*
python -m lifeapi.scraper "$@"
