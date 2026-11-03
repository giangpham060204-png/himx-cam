#!/usr/bin/env bash
# Stop the L3a host/API smoke processes only.
set +e
RUNTIME="/tmp/opencode/arm-moonraker"
for name in moonraker klippy input; do
  f="$RUNTIME/$name.pid"
  if [ -f "$f" ]; then
    kill "$(cat "$f")" 2>/dev/null
    rm -f "$f"
  fi
done
rm -f "$RUNTIME/klippy_uds" "$RUNTIME/input"
echo "L3a host/API smoke stopped"
