#!/bin/sh
# Container replacement for the launchd jobs: scrape now, then every
# LIFEAPI_SCRAPE_INTERVAL seconds (default 2 h), measured from the end of each run. In
# between, every LIFEAPI_SYNC_POLL seconds (default 10), it runs any manual sync requests
# (POST /sync), which the API signals by creating $DATA/sync-requested.
INTERVAL="${LIFEAPI_SCRAPE_INTERVAL:-7200}"
POLL="${LIFEAPI_SYNC_POLL:-10}"
TRIGGER="${LIFEAPI_DATA_DIR:-/data}/sync-requested"
# healthcheck.sh reads this. It's touched when the loop starts and after each run, so a
# stale file means a run has hung.
HEARTBEAT=/tmp/scrape-loop.heartbeat
trap 'exit 0' TERM INT

touch "$HEARTBEAT"
while :; do
    /app/deploy/container/scrape.sh "$@" || echo "scrape-loop: run exited with $?"
    touch "$HEARTBEAT"
    NEXT=$(( $(date +%s) + INTERVAL ))
    while [ "$(date +%s)" -lt "$NEXT" ]; do
        if [ -e "$TRIGGER" ]; then
            /app/deploy/container/scrape.sh --requested || echo "scrape-loop: requested run exited with $?"
            touch "$HEARTBEAT"
        fi
        sleep "$POLL" &
        wait $!
    done
done
