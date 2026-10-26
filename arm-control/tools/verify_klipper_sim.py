#!/usr/bin/env python3
"""Run the generated config through Klipper's host simulator."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.klipper_config import generate_printer_config  # noqa: E402

TEST_GCODE = """\
MANUAL_STEPPER STEPPER=m1 SET_POSITION=0
MANUAL_STEPPER STEPPER=m1 MOVE=10 SPEED=5 ACCEL=50
MANUAL_STEPPER STEPPER=m2 SET_POSITION=0
MANUAL_STEPPER STEPPER=m2 MOVE=10 SPEED=5 ACCEL=50
MANUAL_STEPPER STEPPER=m3 SET_POSITION=0
MANUAL_STEPPER STEPPER=m3 MOVE=5 SPEED=5 ACCEL=50
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--klipper-dir",
        default=os.environ.get("KLIPPER_DIR", ""),
        help="Klipper checkout with out/klipper.dict",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "src" / "config" / "arm.toml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    klipper = Path(args.klipper_dir).expanduser()
    klippy = klipper / "klippy" / "klippy.py"
    dictionary = klipper / "out" / "klipper.dict"
    parsedump = klipper / "klippy" / "parsedump.py"

    if not klippy.is_file():
        print(f"missing {klippy}; set --klipper-dir or KLIPPER_DIR", file=sys.stderr)
        return 1
    if not dictionary.is_file():
        print(f"missing {dictionary}; build the host simulator target", file=sys.stderr)
        return 1
    if not parsedump.is_file():
        print(f"missing {parsedump}", file=sys.stderr)
        return 1

    config = load_config(args.config)

    with tempfile.TemporaryDirectory(prefix="arm-klipper-sim-") as tmp:
        tmp_path = Path(tmp)
        printer_cfg = tmp_path / "printer.cfg"
        printer_cfg.write_text(
            generate_printer_config(config, include_tmc=False)
        )
        gcode = tmp_path / "test.gcode"
        gcode.write_text(TEST_GCODE)
        serial = tmp_path / "test.serial"

        run = subprocess.run(
            [
                sys.executable,
                str(klippy),
                str(printer_cfg),
                "-i",
                str(gcode),
                "-o",
                str(serial),
                "-v",
                "-d",
                str(dictionary),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if run.returncode != 0:
            print(run.stdout, file=sys.stderr)
            print(run.stderr, file=sys.stderr)
            return run.returncode

        dump = subprocess.run(
            [sys.executable, str(parsedump), str(dictionary), str(serial)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(dump.stdout)
        if dump.returncode != 0:
            print(dump.stderr, file=sys.stderr)
            return dump.returncode
        if "queue_step" not in dump.stdout:
            print("no queue_step in parsed output", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
