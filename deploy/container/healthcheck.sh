#!/bin/sh
# Heartbeat healthcheck: healthcheck.sh [HEARTBEAT_FILE] [MAX_AGE_SECONDS]
#
# Defaults suit the scraper: unhealthy once the loop's heartbeat is older than one
# interval plus LIFEAPI_SCRAPE_MAX_RUN seconds (default 1 h), i.e. a run has hung. Failing
# sources don't make it unhealthy; those show up in the API's /sources.
INTERVAL="${LIFEAPI_SCRAPE_INTERVAL:-7200}"
MAX_RUN="${LIFEAPI_SCRAPE_MAX_RUN:-3600}"
HEARTBEAT="${1:-/tmp/scrape-loop.heartbeat}"
MAX_AGE="${2:-$(( INTERVAL + MAX_RUN ))}"

[ -f "$HEARTBEAT" ] || { echo "no heartbeat yet"; exit 1; }
AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT") ))
if [ "$AGE" -gt "$MAX_AGE" ]; then
    echo "heartbeat is ${AGE}s old"
    exit 1
fi
echo "ok (heartbeat ${AGE}s old)"
