#!/usr/bin/env bash
# Serves the Mainsail v2.19.0 diagnostics SPA from mainsail_data/dist on
# 127.0.0.1:8757. Diagnostics ONLY: klippy state, console, machine info.
# Arm motion stays in arm_web (:8765) / CLI; Mainsail never moves the arm.
#
# Requirements (see arm-control/documentations/mainsail-panel.md):
# - ~/projects/himx-cam/mainsail_data/dist/config.json has "port": 7125
# - printer_data/config/moonraker.conf [authorization] cors_domains lists
#   http://127.0.0.1:8757  (and http://localhost:8757 for SSH tunnels)
# - browse at http://127.0.0.1:8757 (IP, not "localhost", for same-host
#   CORS fallback; for tunnels "localhost" works as long as both 8757 and
#   7125 are forwarded)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="/tmp/opencode/arm-real"
LOG_DIR="$ROOT/logs"
mkdir -p "$RUNTIME" "$LOG_DIR"

"$ROOT/stop_mainsail.sh" >/dev/null 2>&1 || true

if [ ! -f "$ROOT/dist/index.html" ]; then
  echo "mainsail: $ROOT/dist/index.html missing; install Mainsail first" >&2
  echo "mainsail: (see arm-control/documentations/mainsail-panel.md)" >&2
  exit 1
fi

if ! grep -q '"port": 7125' "$ROOT/dist/config.json" 2>/dev/null; then
  echo "mainsail: WARN dist/config.json does not pin Moonraker port 7125" >&2
  echo "mainsail:      Mainsail will target its own page port and fail." >&2
fi

setsid python3 -m http.server 8757 --bind 127.0.0.1 --directory "$ROOT/dist" \
  >"$LOG_DIR/mainsail_http.log" 2>&1 &
echo $! > "$RUNTIME/mainsail_http.pid"

for _ in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8757/ || true)
  if [ "$code" = "200" ]; then
    echo "mainsail: serving http://127.0.0.1:8757 (Moonraker ws :7125)"
    exit 0
  fi
  sleep 0.5
done

echo "mainsail: :8757 did not answer 200; see $LOG_DIR/mainsail_http.log" >&2
exit 1
