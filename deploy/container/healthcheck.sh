#!/bin/sh
# Scraper healthcheck: unhealthy once the loop's heartbeat is older than
# LIFEAPI_SCRAPE_MAX_RUN seconds (default 1 h) plus two minutes (the loop touches it every
# minute when idle), i.e. a run has hung. Failing sources don't make it unhealthy; those
# show up in the API's /sources.
MAX_RUN="${LIFEAPI_SCRAPE_MAX_RUN:-3600}"
HEARTBEAT=/tmp/scrape-loop.heartbeat

[ -f "$HEARTBEAT" ] || { echo "no heartbeat yet"; exit 1; }
AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT") ))
if [ "$AGE" -gt $(( MAX_RUN + 120 )) ]; then
    echo "heartbeat is ${AGE}s old"
    exit 1
fi
echo "ok (heartbeat ${AGE}s old)"
