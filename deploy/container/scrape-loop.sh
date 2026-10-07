#!/bin/sh
# Container replacement for the launchd jobs. Every minute it runs whatever is due on its
# schedule (`--due`: each source's interval, set through the API, else
# LIFEAPI_SCRAPE_INTERVAL seconds, default 2 h), which at startup is every source that
# hasn't run within its interval. In between, every LIFEAPI_SYNC_POLL seconds (default 10),
# it runs any manual sync requests (POST /sync), which the API signals by creating
# $DATA/sync-requested.
CHECK=60
POLL="${LIFEAPI_SYNC_POLL:-10}"
TRIGGER="${LIFEAPI_DATA_DIR:-/data}/sync-requested"
# healthcheck.sh reads this. It's touched when the loop starts and after each run (at
# least once a minute when idle), so a stale file means a run has hung.
HEARTBEAT=/tmp/scrape-loop.heartbeat
trap 'exit 0' TERM INT

touch "$HEARTBEAT"
while :; do
    /app/deploy/container/scrape.sh --due "$@" || echo "scrape-loop: scheduled run exited with $?"
    touch "$HEARTBEAT"
    NEXT=$(( $(date +%s) + CHECK ))
    while [ "$(date +%s)" -lt "$NEXT" ]; do
        if [ -e "$TRIGGER" ]; then
            /app/deploy/container/scrape.sh --requested || echo "scrape-loop: requested run exited with $?"
            touch "$HEARTBEAT"
        fi
        sleep "$POLL" &
        wait $!
    done
done
