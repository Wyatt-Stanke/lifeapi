#!/bin/sh
# Scraper healthcheck: unhealthy once the loop's heartbeat is older than one interval
# plus LIFEAPI_SCRAPE_MAX_RUN seconds (default 1 h), i.e. a run has hung. Failing
# sources don't make it unhealthy; those show up in the API's /sources.
INTERVAL="${LIFEAPI_SCRAPE_INTERVAL:-7200}"
MAX_RUN="${LIFEAPI_SCRAPE_MAX_RUN:-3600}"
HEARTBEAT=/tmp/scrape-loop.heartbeat

[ -f "$HEARTBEAT" ] || { echo "no heartbeat yet"; exit 1; }
AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT") ))
if [ "$AGE" -gt $(( INTERVAL + MAX_RUN )) ]; then
    echo "heartbeat is ${AGE}s old"
    exit 1
fi
echo "ok (heartbeat ${AGE}s old)"
