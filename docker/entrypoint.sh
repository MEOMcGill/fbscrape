#!/usr/bin/env bash
set -euo pipefail

DISPLAY_NUM="${DISPLAY:-:99}"
RES="${SCREEN_RES:-1280x800x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

# Strip the leading colon to get the bare display number (e.g. ":99" -> "99").
DISPLAY_NUMERIC="${DISPLAY_NUM#:}"

# Clean up stale X locks left behind by a previous container run (Docker
# preserves the writable layer when you `up -d` an already-existing stopped
# container, so /tmp/.X{N}-lock and /tmp/.X11-unix/X{N} can carry over and
# block Xvfb from claiming the display).
rm -f "/tmp/.X${DISPLAY_NUMERIC}-lock" \
      "/tmp/.X11-unix/X${DISPLAY_NUMERIC}" 2>/dev/null || true

Xvfb "$DISPLAY_NUM" -screen 0 "$RES" -ac +extension RANDR -nolisten tcp &
fluxbox -display "$DISPLAY_NUM" >/dev/null 2>&1 &

for _ in $(seq 1 30); do
    if xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1; then break; fi
    sleep 0.1
done

x11vnc -display "$DISPLAY_NUM" -nopw -forever -shared \
       -rfbport "$VNC_PORT" -bg -quiet -o /tmp/x11vnc.log

websockify --web=/usr/share/novnc "$NOVNC_PORT" "localhost:$VNC_PORT" \
       >/tmp/websockify.log 2>&1 &

echo "noVNC: http://localhost:${NOVNC_PORT}/vnc.html (display ${DISPLAY_NUM}, ${RES})"

exec "$@"
