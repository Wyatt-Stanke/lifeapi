#!/bin/sh
# Finish a Google / College Board sign-in challenge by hand: a headed scrape on a virtual
# display, served over VNC on port 5900. Run it inside the scraper container:
#
#   VNC_PASSWORD=... /app/deploy/container/login.sh --only google_classroom
#
# It waits for any scheduled run to finish first (they share the profile lock).
set -e
: "${VNC_PASSWORD:?set VNC_PASSWORD (the VNC server listens on the container network)}"
export DISPLAY=:99
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
XVFB=$!
sleep 1
x11vnc -display :99 -forever -shared -quiet -passwd "$VNC_PASSWORD" -rfbport 5900 &
VNC=$!
trap 'kill $VNC $XVFB 2>/dev/null' EXIT
echo "login.sh: connect a VNC client to port 5900 and finish the sign-in in the Chrome window."
/app/deploy/container/scrape.sh --headed "$@"
