#!/bin/sh
# One scraper run. Arguments go to `python -m lifeapi.scraper`. It waits for the browser
# profile lock (browser.py), so it never overlaps the files worker or login.sh.
exec python -m lifeapi.scraper "$@"
