#!/usr/bin/env bash
# Stops the Mainsail static server started by start_mainsail.sh.
set -euo pipefail
RUNTIME="/tmp/opencode/arm-real"

stopped=""
if [ -f "$RUNTIME/mainsail_http.pid" ]; then
  PID="$(cat "$RUNTIME/mainsail_http.pid")"
  if kill "$PID" 2>/dev/null; then
    for _ in $(seq 1 10); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.2
    done
    kill -9 "$PID" 2>/dev/null || true
  fi
  rm -f "$RUNTIME/mainsail_http.pid"
  stopped="yes"
fi

# Belt & suspenders: any stray listener on :8757 by exact command match.
# The [h] trick keeps grep from matching its own ps line (AGENTS.md rule).
for pid in $(ps -eo pid,args | grep "[h]ttp.server 8757" | awk '{print $1}'); do
  kill "$pid" 2>/dev/null && stopped="yes"
done

if [ -n "$stopped" ]; then
  echo "mainsail: stopped"
else
  echo "mainsail: nothing to stop"
fi
