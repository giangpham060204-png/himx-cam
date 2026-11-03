#!/usr/bin/env bash
# P1 host stack: real Klipper (~/klipper-pico) talking to the real Pico over
# USB CDC + Moonraker. This is NOT the simulator: klippy is launched with no
# -i/-o/-d debug flags, so it opens the [mcu] serial port from printer.cfg.
set -euo pipefail
# Derive the root from this script's own location (it lives in printer_data).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="/tmp/opencode/arm-real"
mkdir -p "$RUNTIME" "$ROOT/logs"
"$ROOT/stop_host_real.sh" >/dev/null 2>&1 || true
rm -f "$RUNTIME/klippy_uds" "$RUNTIME/klippy.log"
echo "=== Preflight ==="
MCU_SERIAL="$(sed -n 's/^serial: *//p' "$ROOT/config/printer.cfg" | head -1)"
if [ ! -e "$MCU_SERIAL" ]; then
  echo "arm-real: MCU serial not present: $MCU_SERIAL" >&2
  echo "arm-real: is the Pico flashed and plugged in? ls /dev/serial/by-id/" >&2
  exit 1
fi
echo "  mcu serial: $MCU_SERIAL"
# klippy must be able to open the CDC port (dialout group).
if [ ! -r "$MCU_SERIAL" ] || [ ! -w "$MCU_SERIAL" ]; then
  echo "arm-real: no read/write on $MCU_SERIAL (need dialout; try sg dialout -c ...)" >&2
  exit 1
fi
echo "  serial access: ok"
# Launch klippy against the real MCU. No -i/-o/-d: it uses [mcu] serial.
setsid "$HOME/klippy-env/bin/python" \
  "$HOME/klipper-pico/klippy/klippy.py" \
  "$ROOT/config/printer.cfg" \
  -a "$RUNTIME/klippy_uds" \
  -l "$RUNTIME/klippy.log" \
  >"$RUNTIME/klippy.out" 2>"$RUNTIME/klippy.err" &
echo $! > "$RUNTIME/klippy.pid"
for _ in $(seq 1 60); do
  [ -S "$RUNTIME/klippy_uds" ] && break
  sleep 0.2
done
if [ ! -S "$RUNTIME/klippy_uds" ]; then
  echo "arm-real: klippy_uds did not appear; see $RUNTIME/klippy.err and $RUNTIME/klippy.log" >&2
  tail -20 "$RUNTIME/klippy.log" 2>/dev/null >&2 || true
  exit 1
fi
cd "$HOME/moonraker"
# Moonraker's authorization component needs libsodium; fall back to a
# user-space copy extracted from the .deb (see documentations/how-to-test.md).
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
READY=""
for _ in $(seq 1 100); do
  if curl -fsS --max-time 1 http://127.0.0.1:7125/printer/info 2>/dev/null | grep -q '"state":"ready"'; then
    READY=1
    break
  fi
  sleep 0.2
done
if [ -n "$READY" ]; then
  echo "P1 real host stack READY on http://127.0.0.1:7125 (klippy <-> real Pico)"
else
  echo "arm-real: Moonraker up but klippy not ready; see $RUNTIME/klippy.log" >&2
  curl -fsS --max-time 2 http://127.0.0.1:7125/printer/info 2>/dev/null || true
  echo >&2
  exit 1
fi
