#!/bin/sh
# Container replacement for the launchd job: scrape now, then every
# LIFEAPI_SCRAPE_INTERVAL seconds (default 2 h), measured from the end of each run.
INTERVAL="${LIFEAPI_SCRAPE_INTERVAL:-7200}"
# healthcheck.sh reads this. It's touched when the loop starts and after each run, so a
# stale file means a run has hung.
HEARTBEAT=/tmp/scrape-loop.heartbeat
trap 'exit 0' TERM INT

touch "$HEARTBEAT"
while :; do
    /app/deploy/container/scrape.sh "$@" || echo "scrape-loop: run exited with $?"
    touch "$HEARTBEAT"
    sleep "$INTERVAL" &
    wait $!
done
