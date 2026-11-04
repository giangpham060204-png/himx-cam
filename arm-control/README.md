# Arm control

Host-side control for the 3-DOF camera arm. Standard-library Python, Klipper
`kinematics: none` + `manual_stepper`, Moonraker over HTTP, and a mock-test
suite that runs without hardware.

## Layout

```text
arm-control/
├── src/        Python package (config, kinematics, commands, controller)
├── tools/              operator CLI, bench CLI, web console, Klipper simulator harness
├── web/                local console HTML/CSS/JS
├── tests/              arm-only pytest suite
├── documentations/     specs, design, test guide, bench checklist (local)
└── sketches/           arm/RCM sketches (local)
```

`documentations/` and `sketches/` are local-only by project convention.
Plans and specs live under `../docs/superpowers/`.

## Quick start

From the repository root:

```bash
uv sync
uv run pytest arm-control/tests -q
uv run python arm-control/tools/arm_bench.py limits
```

Expected: the arm-only suite passes, with the Klipper simulator test skipped
unless `KLIPPER_DIR` is set; `limits` prints accepted/refused checks and exits
0.

Generate the Klipper config:

```bash
PYTHONPATH=arm-control uv run python - <<'PY'
from pathlib import Path
from src.config import load_config
from src.klipper_config import write_printer_config

config = load_config("arm-control/src/config/arm.toml")
write_printer_config(config, Path("printer.cfg"))
print("wrote printer.cfg")
PY
```

Run the operator CLI or bench CLI:

```bash
uv run python arm-control/tools/arm_cli.py --help
uv run python arm-control/tools/arm_bench.py --help
```

Run the local web console:

```bash
uv run python arm-control/tools/arm_web.py --open
# or open http://127.0.0.1:8765/
```

The web console binds to `127.0.0.1` by default, uses a per-run token, and
sends every motion through the same `ArmController` safety layer as the CLI.
It is a controller surface with M1 up/down, M2 left/right, M3 left/right,
HOME, and an emergency-stop button. HOME is wired to `/api/home`
(`home_all`); it respects the same guards as the CLI (M2 is never homed
from an unknown multi-turn cable state). The physical e-stop remains the
primary stop.

The status line also shows the live klippy state (`klipper: ready`,
`offline`, `shutdown`, …) plus a fault reason. When klippy is down
(e-stop latch, shutdown, or fault), a RECOVER button appears next to it;
RECOVER calls `/api/restart` (`controller.restart()`: Moonraker
`firmware_restart`, wait-ready, clear latched stop/fault — same as
`arm_cli.py restart`), so an e-stop can be undone from the browser alone.

Mainsail on the same host is a diagnostics window only
(console, endstops, logs, configs); it is not used to move the arm — stock
Mainsail and Fluidd cannot host custom panels (verified against v2.19.0
sources; see `documentations/mainsail-panel.md`), so the console is the
single control surface and the panel prototype stays parked in
`web/mainsail/`.

The default Moonraker URL is `http://127.0.0.1:7125`. The default config is
`arm-control/src/config/arm.toml`; the serial path in it is a
placeholder until the Pico is flashed.

## Testing

Read `documentations/how-to-test.md` for the L0–L6 ladder, exact commands,
expected output, and troubleshooting. Read
`documentations/simulation-wiring-guide.md` for the combined simulation,
Pico/TMC2209 wiring, and first-power-up guide. Read
`documentations/bench-checklist.md` before any real motor test.

## Mainsail diagnostics window

Mainsail is installed on the Moonraker host (see
`documentations/mainsail-panel.md`, local) and is used read-mostly:
G-code console (`QUERY_ENDSTOPS`, `MANUAL_STEPPER` probes), Klippy/Moonraker
logs, MCU info, config editor. Its toolhead control is disabled;
`manual_stepper` axes are invisible to Moonraker, so Mainsail cannot move
or read the arm. Arm control lives in this repo's console and (Phase 1)
the Mainsail arm panel, both talking to `arm_web.py`. Mainsail's own
EMERGENCY-STOP button sends raw `M112` around `ArmController` (no audit,
no safety-lane bookkeeping), so it is hidden with an inline CSS patch in
`mainsail_data/dist/index.html`; the toolbar restart menu (⏻) stays as a
manual recovery path. Reapply that patch after a Mainsail upgrade.

## Safety

- No hardware has been tested.
- Real motion requires a clear bench, a latching e-stop, a current-limited
  24 V supply, and the bench checklist.
- A 24 V-only e-stop is not visible to the host unless an auxiliary contact is
  wired and tested.
- M2 must not be homed from an unknown multi-turn cable state; confirm the
  cable state or fit a slip ring first.
- Phase D (`GCODE_AXIS` coordinated motion) is not implemented.
