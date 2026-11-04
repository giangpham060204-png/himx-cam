
## Pico/USB/STM32/RP2040 + Klipper safety notes (Nov 2026)

### TMC2209 config crash wedge
- **Never** generate/start Klipper with `[tmc2209 manual_stepper ...]` sections while the TMC drivers are unpowered/not wired. If Klipper fails during TMC UART init (e.g. `Unable to read tmc uart ... register IFCNT`), it can hard-shutdown and wedge the RP2040 USB stack into the ROM BOOTSEL bootloader (`2e8a:0003`) even though the firmware remains flashed.
- **For P1 (Pico + USB only):** generate config with `include_tmc=False`:
  ```bash
  cd ~/projects/himx-cam
  PYTHONPATH=arm-control uv run python - <<'PY'
  from pathlib import Path
  from src.config import load_config
  from src.klipper_config import write_printer_config
  config = load_config("arm-control/src/config/arm.toml")
  out = Path("/home/commongdc/projects/himx-cam/printer_data/config/printer.cfg")
  write_printer_config(config, out, include_tmc=False, force=True)
  print("wrote", out)
  PY
  ```
- **For P2+:** only enable TMC sections when drivers are powered (24V), wired correctly, and motors disconnected. Test configs standalone first before starting the full stack.
- **Recovery:** if stuck in BOOTSEL (`2e8a:0003`, no `/dev/ttyACM*`/serial by-id), flash the UF2 from `~/klipper-pico/out/klipper.uf2` with `rp2040_flash` (device will appear as mass storage or be detected). If it won't flash, physical power cycle/reset may be needed, but usually flashing the UF2 while in BOOTSEL works. After flashing, it re-enumerates as Klipper RP2040.

### stk500v2 hang on RP2040
- Klipper's `serialhdl.py` does an AVR stk500v2 leave sequence that can hang on RP2040; we patched it in `~/klipper-pico/klippy/serialhdl.py` to skip on failure.

### Real stack paths
- Use `~/projects/himx-cam/printer_data/start_host_real.sh` / `stop_host_real.sh`. Runtime in `/tmp/opencode/arm-real/`. Moonraker needs `klippy_uds_address` pointing to the same runtime dir.

### Stale processes masquerading as config problems (Nov 2026)
- **Symptoms that look like "config not loaded" but are process soup:** `MANUAL_STEPPER` -> `Unknown command`, `/printer/objects/list` missing `manual_stepper m1/m2/m3`, API answers contradicting the log. Cause: leftover klippy/moonraker instances (one session had a day-old Moonraker holding :7125 plus an orphaned klippy holding the serial port).
- **Orphan trap:** starting klippy via `sg dialout -c "..." &` then `kill $!` kills the `sg` wrapper only; the python child keeps the serial port. New klippy then fails with `Could not exclusively lock port ... Resource temporarily unavailable` / `MCU error during connect`. Kill the actual python PIDs (check `ps -eo pid,args | grep klippy.py`).
- **`pkill -f` self-match:** `pkill -9 -f 'klippy.py'` also matches the shell running the command (pattern text is in its own cmdline) and SIGKILLs your own shell. Use a self-safe pattern like `pkill -9 -f 'klippy/[k]lippy'` or kill by PID.
- **Clean start recipe:** kill every `klippy.py` / `moonraker.server` process, verify `fuser /dev/ttyACM0` empty and port 7125 free, then run `printer_data/start_host_real.sh` once. A single klippy + single moonraker is the only trustworthy state.
- **`manual_stepper` never appears in `/printer/objects/list`:** by design — klippy filters objects without `get_status`, and `ManualStepper` has none. arm-control works around it (`has_object()` queries directly). To prove the objects exist, send `MANUAL_STEPPER STEPPER=badname MOVE=1` and read the mux error: `Options: 'm1', 'm2', 'm3'`.
- **Moonraker HTTP `objects/query` format:** each query-string KEY is an object name (`?mcu=` all fields, `?mcu=bytes_write,send_seq` for fields). Passing `?objects=mcu` yields the useless `status.objects.<name>: null` shape.
- **`QUERY_ENDSTOPS` output is ONE joined line** (`m1:TRIGGERED m2:TRIGGERED ...`) via `respond_raw` — parsers must scan for all pairs, not match one pair per line (fixed in `arm-control/src/moonraker.py::query_endstops_text`).
- **E-stop recovery:** `bench estop` puts klippy in `shutdown`; recover with `arm_cli.py restart` (dispatches before connect by design, waits for ready). If klippy is shut down and the CLI refuses everything, raw fallback: `curl -X POST http://127.0.0.1:7125/printer/firmware_restart`.
- **`motion_report.live_position` stays 0 for manual_steppers — by design:** `motion_report.py::get_status` only samples the toolhead trapq plus gcode-registered extra axes (`GCODE_AXIS`), never plain manual_stepper trapqs (they appear only in the `trapq`/`steppers` name lists). To observe real-time manual_stepper motion, subscribe to `motion_report/dump_trapq` and `motion_report/dump_stepper` (mux key `name=manual_stepper m1`) on klippy's webhooks unix socket (`/tmp/opencode/arm-real/klippy_uds`). Protocol: JSON messages terminated by `\x03` both directions (NOT newline). Expected proof: trapq shows the accel/cruise/decel trapezoid segments; dump_stepper step counts match `distance * full_steps*microsteps/rotation_distance` exactly (e.g. m1: 400 steps/mm → 10mm = 4000 steps; m2: 8.888 steps/deg → 30° = 267; m3: 400 steps/mm → 5mm = 2000).

### Mainsail diagnostics window (Nov 2026)
- Mainsail v2.19.0 dist at `mainsail_data/dist` (vendored, gitignored). Serve with `mainsail_data/start_mainsail.sh` (backgrounds `python3 -m http.server 8757 --bind 127.0.0.1 --directory mainsail_data/dist`, PID file `/tmp/opencode/arm-real/mainsail_http.pid`, log `mainsail_data/logs/mainsail_http.log`); stop with `mainsail_data/stop_mainsail.sh`. Page URL must be an IP origin `http://127.0.0.1:8757` (Moonraker's CORS fallback only accepts origins whose host is a trusted IP); for SSH tunnels forward 8757 **and** 7125 and browse `http://localhost:8757` — `cors_domains` covers both origins.
- `mainsail_data/dist/config.json` needs `"port": 7125` (else Mainsail targets Moonraker at its own page port and loops "Connection failed").
- `printer_data/config/moonraker.conf` `[authorization] cors_domains` allows ANY localhost / 127.0.0.1 origin port (`http://127.0.0.1:*`, `http://localhost:*`, plus explicit `…:8757`) — REQUIRED: with an empty `cors_domains`, `Authorization.check_cors()` returns False before its trusted-IP fallback (the fallback is inside the pattern loop). Symptom if removed: `403 GET /websocket (127.0.0.1) [_TRUSTED_USER_]` in moonraker.log. Tunnel consequence: the SSH-forwarded page/console ports may be remapped freely, but the **Moonraker local port must stay 7125** (Mainsail targets it via `dist/config.json`).
- Moonraker has no self-restart endpoint. Restart by explicit PID. CAUTION: `pgrep -f "moonraker.server"` ALSO matches your own `bash -c` (the pattern text appears in the launcher command), so `head -1` can kill your own shell instead of Moonraker — one such accidental kill left the OLD Moonraker holding :7125 while a second instance failed to bind. Use `pgrep -f "moonraker-env/bin/python"` to find the real server, kill by PID, verify `ss -tlnp | grep 7125` is free, then relaunch exactly like `start_host_real.sh` lines 50–54 (`-d printer_data -c config/moonraker.conf -l logs/moonraker.log`, LIBSODIUM_DIR exported) and confirm a SINGLE instance + the ACAO header answers for both cors origins.
- Mainsail is diagnostics-only; arm motion stays in arm_web console (:8765) / CLI. "Missing configuration" warning in Mainsail (virtual_sdcard etc.) is expected for the arm config — do not add print-job sections.
- Playwright verification: system node 18 is too old for the npm playwright 1.64; use `/tmp/opencode/arm-real/pw-venv` (`uv venv + uv pip install playwright`, reuses `~/.cache/ms-playwright/chromium-1248`). Screenshots + drivers in `arm-control/documentations/mainsail-panel-shots/`.
