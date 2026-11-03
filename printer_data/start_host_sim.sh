#!/usr/bin/env bash
# L3a host/API smoke: Klipper batch simulator + Moonraker. No Linux MCU/Pico.
set -euo pipefail
# This script lives inside the printer_data tree it drives, so derive the
# root from the script's own location rather than assuming $HOME/printer_data.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="/tmp/opencode/arm-moonraker"
mkdir -p "$RUNTIME" "$ROOT/logs"
"$ROOT/stop_host_sim.sh" >/dev/null 2>&1 || true
rm -f "$RUNTIME/klippy_uds" "$RUNTIME/klippy.log" "$RUNTIME/output.serial"
rm -f "$RUNTIME/input"
mkfifo "$RUNTIME/input"
setsid sleep 100000 > "$RUNTIME/input" &
echo $! > "$RUNTIME/input.pid"
setsid "$HOME/klippy-env/bin/python" \
  "$HOME/klipper-sim-moonraker/klippy/klippy.py" \
  "$ROOT/config/printer.cfg" \
  -i "$RUNTIME/input" \
  -o "$RUNTIME/output.serial" \
  -d "$HOME/klipper/out/klipper.dict" \
  -a "$RUNTIME/klippy_uds" \
  -l "$RUNTIME/klippy.log" \
  >"$RUNTIME/klippy.out" 2>"$RUNTIME/klippy.err" &
echo $! > "$RUNTIME/klippy.pid"
for _ in $(seq 1 40); do
  [ -S "$RUNTIME/klippy_uds" ] && break
  sleep 0.1
done
cd "$HOME/moonraker"
# Moonraker's authorization component imports libnacl, which needs the system
# libsodium shared library. It is not installed system-wide here and installing
# it needs sudo, so fall back to a user-space copy extracted from the .deb
# (see documentations/how-to-test.md). If a system libsodium exists, this is a
# no-op beyond prepending an already-valid path.
LIBSODIUM_DIR="$HOME/opt/libsodium/usr/lib/x86_64-linux-gnu"
if [ -e "$LIBSODIUM_DIR/libsodium.so.23" ]; then
  export LD_LIBRARY_PATH="$LIBSODIUM_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
setsid "$HOME/moonraker-env/bin/python" -c 'from moonraker.server import main; main()' \
  -d "$ROOT" \
  -c "$ROOT/config/moonraker.conf" \
  -l "$ROOT/logs/moonraker.log" \
  >"$RUNTIME/moonraker.out" 2>"$RUNTIME/moonraker.err" &
echo $! > "$RUNTIME/moonraker.pid"
for _ in $(seq 1 80); do
  if curl -fsS --max-time 1 http://127.0.0.1:7125/printer/info 2>/dev/null | grep -q '"state":"ready"'; then
    break
  fi
  sleep 0.1
done
curl -fsS --max-time 2 http://127.0.0.1:7125/printer/info >/dev/null
echo "L3a host/API smoke ready on http://127.0.0.1:7125"
