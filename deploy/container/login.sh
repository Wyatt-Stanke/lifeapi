#!/bin/sh
# Finish a Google / College Board sign-in challenge by hand: a headed scrape on a virtual
# display, served over VNC. You normally don't run this directly: `deploy/reauth.py` on
# your own machine starts it over SSH and tunnels the VNC session back to you.
#
#   /app/deploy/container/login.sh --only google_classroom
#
# VNC listens on the container's loopback only (port 5900), so nothing on the container
# network can reach it; the way in is `docker exec`. The password is random for each
# session unless VNC_PASSWORD is set (VNC uses at most 8 characters). It's passed to
# x11vnc through a file that x11vnc deletes, never on a command line.
#
# With LIFEAPI_LOGIN_STOP_ON_EOF=1, closing stdin (reauth.py exiting, or its SSH
# connection dropping) stops the scrape and the display. It waits for any scheduled run
# to finish first (they share the profile lock).
set -e
VNC_PASSWORD="${VNC_PASSWORD:-$(python -c 'import secrets; print(secrets.token_urlsafe(6))')}"
PASSFILE=$(mktemp)
printf '%s\n' "$VNC_PASSWORD" >"$PASSFILE"

export DISPLAY=:99
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp 2>/dev/null &
XVFB=$!
sleep 1
x11vnc -display :99 -localhost -rfbport 5900 -forever -shared -quiet -passwdfile "rm:$PASSFILE" \
    >/tmp/x11vnc.log 2>&1 &
VNC=$!
SCRAPE=
WATCH=

cleanup() {
    # The scrape and the stdin watcher run in their own sessions (setsid), so their whole
    # process group (Python, Chrome) goes.
    [ -n "$SCRAPE" ] && kill -TERM "-$SCRAPE" 2>/dev/null
    [ -n "$WATCH" ] && kill -TERM "-$WATCH" 2>/dev/null
    kill $VNC $XVFB 2>/dev/null
    rm -f "$PASSFILE"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

python - <<'EOF' || { echo "login.sh: x11vnc didn't start:" >&2; cat /tmp/x11vnc.log >&2; exit 1; }
import socket, time
for _ in range(50):
    try:
        socket.create_connection(("127.0.0.1", 5900), timeout=1).close()
        break
    except OSError:
        time.sleep(0.2)
else:
    raise SystemExit(1)
EOF
echo "login.sh: VNC password: $VNC_PASSWORD"
echo "login.sh: VNC ready on 127.0.0.1:5900. Finish the sign-in in the Chrome window."

set +e
setsid /app/deploy/container/scrape.sh --headed "$@" &
SCRAPE=$!
if [ -n "$LIFEAPI_LOGIN_STOP_ON_EOF" ]; then
    # A background job's stdin is /dev/null unless redirected, so hand it ours explicitly.
    exec 3<&0
    setsid sh -c 'cat >/dev/null; echo "login.sh: disconnected, stopping"; kill -TERM "-$1"' \
        sh "$SCRAPE" <&3 &
    WATCH=$!
    exec 3<&-
fi
wait $SCRAPE
STATUS=$?
SCRAPE=
echo "login.sh: scrape exited with $STATUS"
exit $STATUS
