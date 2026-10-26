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
and an emergency-stop button. Home the arm with the CLI/bench first; there is no home button. The
physical e-stop remains the primary stop.

The default Moonraker URL is `http://127.0.0.1:7125`. The default config is
`arm-control/src/config/arm.toml`; the serial path in it is a
placeholder until the Pico is flashed.

## Testing

Read `documentations/how-to-test.md` for the L0–L6 ladder, exact commands,
expected output, and troubleshooting. Read
`documentations/simulation-wiring-guide.md` for the combined simulation,
Pico/TMC2209 wiring, and first-power-up guide. Read
`documentations/bench-checklist.md` before any real motor test.

## Safety

- No hardware has been tested.
- Real motion requires a clear bench, a latching e-stop, a current-limited
  24 V supply, and the bench checklist.
- A 24 V-only e-stop is not visible to the host unless an auxiliary contact is
  wired and tested.
- M2 must not be homed from an unknown multi-turn cable state; confirm the
  cable state or fit a slip ring first.
- Phase D (`GCODE_AXIS` coordinated motion) is not implemented.
