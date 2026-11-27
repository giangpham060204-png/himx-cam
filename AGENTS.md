
## Environment / permissions (Nov 2026)
- This host is a **remote server; the operator is NOT admin**. **Never install anything** (`apt`, `pip --system`, toolchain, SDKs, OTA firmware deps) **without explicit prior approval** — propose the install command and wait.
- Preferred: project-local, user-space only (`uv`, `thirdparty/` under the repo, `/tmp/opencode`). If a task needs a compiler/SDK that is not present, stop and ask instead of fetching it.
- **Blast-radius rule:** this machine is shared (docker containers, other users/services, long-running arm stack). Before any command that reaches outside the repo + `/tmp/opencode` + the operator's own processes, stop and ask. This includes: `docker` (any subcommand), `systemctl`/service restarts, `reboot`/`shutdown`, firewall/network changes, mounts, `chmod/chown` outside the repo, `kill`/`pkill` of anything not provably ours (note the `pkill -f` self-match trap below), anything binding a fixed port, large downloads/writes (disk-fill risk), and any `sudo` at all.
- **Compatibility check:** `klipper-pico`, `moonraker-env`, `mainsail_data/dist` are pinned working trees — no upgrades, checkouts, or rebuilds without approval. Verify commands against the pinned versions in-tree (don't assume upstream docs match).

### Operator desktop (xpra session `:100`)
- The operator works on this host through an **xpra desktop on `:100`** (`xfce4-session`), attached from their laptop over SSH. Slicer 5.12.3 lives at `~/Downloads/Slicer-5.12.3-linux-amd64/Slicer`.
- The host also runs a **GNOME session on `:0`**; both share `/run/user/1000/bus`. Its GTK portal opens `:0` with the mutter Xauthority, so portal calls made from `:100` failed with `GDBus.Error:org.gtk.GDBus.UnmappedGError…Failed to open display ":100"`.
- Fixed 2026-11-16 by restarting `:100` with its own session bus. `dbus-launch` is **not installed**, so xpra is started with `--dbus-launch=/tmp/opencode/dbus-launch-shim.sh` (a 6-line `dbus-daemon --session --fork --print-address` wrapper emitting sh-syntax). The shim lives in `/tmp`; re-create it or move it somewhere permanent before relying on it after a reboot.

## Himx camera Pico / custom UVC firmware (Nov 2026)

### Identity and flashing
- The **camera Pico** is separate from the arm/Klipper Pico. It now runs the custom `camera_firmware` UVC test build: USB `cafe:4021`, CDC by-id `usb-HimxCam...`, video `/dev/video0`/`/dev/video1`. MicroPython is gone from it.
- The **arm Pico** is `1d50:614e`, serial `E66164084383A533`, `/dev/ttyACM0`. Never flash or reset it by mistake.
- Flashing the camera Pico: CDC command `BOOTSEL` -> `RPI-RP2`; mount with `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus gio mount -d /dev/sda1`; copy the UF2 to `/media/commongdc/RPI-RP2`; `sync`. `udisksctl mount` alone fails (polkit has no TTY). No sudo.

### Blocking I2C hazard (learned the hard way)
- `i2c_read_blocking()` has **no timeout**. On a floating, absent, or stuck bus it can wedge the main loop forever. Symptom: the UVC device still enumerates, but CDC and video stop responding.
- Always use a bounded call: `i2c_read_timeout_per_char_us(..., 1000)` or `i2c_read_blocking_until(...)`. Never scan a floating bus with the blocking variant.
- Build camera firmware with a watchdog: `watchdog_enable(5000, 1)` and `watchdog_update()` in the main loop. A future hang then reboots the Pico instead of requiring physical access.
- **Recovery if the firmware is wedged:** one USB unplug/replug of the camera Pico (no BOOTSEL needed) restarts it. `usbreset`, USB `authorized` toggles, and hub sysfs do **not** reboot the RP2040. After replug, use CDC `BOOTSEL` to flash the fixed UF2.

### Wiring findings
- The safe `LINES` CDC command measures GPIO capacitance with the cameras in standby; it does not stream and does not enable camera outputs.
- The as-built harness now uses a **TCA9548A I2C mux** at address **0x70** on **GP0/GP1**. Cameras are on mux channels **0, 1, 2** and each answers at `0x24` when its channel is selected. This is the preferred design: per-camera configuration is possible.
- Camera signal map (D0, PCLK, HREF, VSYNC in that order):
  - Camera 1: **GP4, GP5, GP6, GP7**
  - Camera 2: **GP8, GP9, GP10, GP11**
  - Camera 3: **GP12, GP13, GP14, GP15**
  - No shared HREF/VSYNC.
- Last `LINES` check (2026-11-12, after a USB replug): all three mux channels 0/1/2 answer `0x24` and read model-id `0x01B0` (`MUXCH0/1/2`). Camera 3 init + stream works (`CAM3`, `cam3=1`); its tile is currently black. Wiring: Cameras 2 and 3 complete; **Camera 1 is missing HREF GP6**. Wiring has moved between checks — re-run `LINES` before trusting an image.
- Firmware now initialises cameras 2 and 3 at boot and fills a tile with video black when its camera is not streaming (`PATTERN 1` restores the diagnostic pattern). Slicer side: module button **Connect real cameras** starts `tools/camera_stream.py --device-tiled /dev/video0` if needed, then connects c1/c2/c3; a missing device streams black.
- **Camera feeds have their own views (layout 812).** `targeting/layouts.py` defines Cam1|Cam2|Cam3 across the top and one wide 3D view below; `CAMERA_VIEWS = ("Cam1","Cam2","Cam3")`, so Red/Green/Yellow stay free for the plate and the legacy click modes. **Camera layout (3 feeds + 3D)** switches; **Connect real cameras** switches on, **Stop real cameras** returns to FourUp. `_fit_camera_row()` sizes the splitter from the pane width (layout XML cannot express proportions) and the camera panes hide their slice controller. The plate's c1 lookup is by volume name (`marker_plate._view_showing`), so it works in Cam1 or Red. Binding refuses by name if the layout is not showing; click filters are installed after the switch.
- The old "channel 2 hangs" symptom was stale mux/bus state from a firmware reset, not a faulty camera. A firmware reset never power-cycles the mux/cameras; only unplugging the camera Pico's USB does.
- The earlier shared-HREF/VSYNC GP15/GP16 wiring is obsolete; the mux design replaces it.
- Do not use the `temp/Cams brief set up.pdf` wiring/code as-is: it is a Google Search AI answer, not official Arducam documentation. Its shared HREF/VSYNC and PIO pseudo-code are not a working three-camera design.
- Files: `camera_firmware/PLAN.md` (design/plan), `camera_firmware/BUILD_LOG.md` (build/flash/diagnostic records). Toolchain: `thirdparty/toolchain/...`; SDK: `thirdparty/pico-sdk-1.5.1`; TinyUSB pinned to `5ce60c5`.

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
- Mainsail's red EMERGENCY-STOP button is HIDDEN by an inline `<style>.emergency-button{display:none!important}</style>` patch in `mainsail_data/dist/index.html` (sends raw M112 around ArmController/audit; misclick shut klippy down with no UI recovery). Reapply after a Mainsail upgrade. Console (:8765) has the audited e-stop plus a klippy status line and `/api/restart` RECOVER chip.
- Playwright verification: system node 18 is too old for the npm playwright 1.64; use `/tmp/opencode/arm-real/pw-venv` (`uv venv + uv pip install playwright`, reuses `~/.cache/ms-playwright/chromium-1248`). Screenshots + drivers in `arm-control/documentations/mainsail-panel-shots/`.
