#!/bin/sh
# Container replacement for the launchd job: scrape now, then every
# LIFEAPI_SCRAPE_INTERVAL seconds (default 2 h), measured from the end of each run.
INTERVAL="${LIFEAPI_SCRAPE_INTERVAL:-7200}"
trap 'exit 0' TERM INT

while :; do
    /app/deploy/container/scrape.sh "$@" || echo "scrape-loop: run exited with $?"
    sleep "$INTERVAL" &
    wait $!
done
