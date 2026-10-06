#!/bin/sh
# Use patchright's Chromium when Google Chrome isn't installed (arm64 images), unless the
# channel was set explicitly.
if [ -z "${LIFEAPI_BROWSER_CHANNEL+x}" ] && [ ! -x /opt/google/chrome/chrome ]; then
    export LIFEAPI_BROWSER_CHANNEL=""
fi
exec "$@"
