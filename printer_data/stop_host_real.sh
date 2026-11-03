#!/usr/bin/env bash
# Stop the P1 real host stack (klippy + Moonraker) only.
set +e
RUNTIME="/tmp/opencode/arm-real"
for name in moonraker klippy; do
  f="$RUNTIME/$name.pid"
  if [ -f "$f" ]; then
    kill "$(cat "$f")" 2>/dev/null
    rm -f "$f"
  fi
done
rm -f "$RUNTIME/klippy_uds"
echo "P1 real host stack stopped"
