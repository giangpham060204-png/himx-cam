#!/usr/bin/env python3
"""Bench checks for the 3-axis arm.

These commands are intentionally thin: the operator still watches the
machine, marks the shaft for one-revolution tests, and signs the checklist.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.audit import AuditLog  # noqa: E402
from src.commands import manual_move  # noqa: E402
from src.config import AXIS_NAMES, load_config  # noqa: E402
from src.controller import ArmController  # noqa: E402
from src.errors import ArmError, OutOfRangeError  # noqa: E402
from src.stopflag import StopFlag  # noqa: E402
from src.moonraker import MoonrakerClient  # noqa: E402
from run_context import build_context  # noqa: E402
from transport_factory import build_transport  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bench checks for the 3-axis arm")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "src" / "config" / "arm.toml",
    )
    parser.add_argument("--url", default="http://127.0.0.1:7125")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--transport", choices=["ws", "http"], default="ws")
    parser.add_argument("--operator", default="")
    parser.add_argument("--hardware-serial", default="")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("endstops")

    home = sub.add_parser("home")
    home.add_argument("axis", choices=["m1", "m2", "m3", "all"])

    jog = sub.add_parser("jog")
    jog.add_argument("axis", choices=["m1", "m2", "m3"])
    jog.add_argument("delta", type=float)
    jog.add_argument("--home-first", action="store_true")

    one_rev = sub.add_parser("one-rev")
    one_rev.add_argument("axis", choices=["m1", "m2", "m3"])

    sub.add_parser("limits")

    tmc = sub.add_parser("tmc")
    tmc.add_argument("axis", choices=["m1", "m2", "m3"])

    sub.add_parser("stop")
    sub.add_parser("resume")
    sub.add_parser("estop")
    return parser


def build_controller(args: argparse.Namespace) -> ArmController:
    config = load_config(args.config)
    log_path = args.log or (
        ROOT
        / "logs"
        / "arm"
        / f"bench-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    )
    audit = AuditLog(log_path, context=build_context(ROOT, args.operator, args.hardware_serial))
    transport = build_transport(args.url, args.transport, args.timeout)
    return ArmController(
        config,
        MoonrakerClient(transport),
        audit,
        stop_flag=StopFlag(ROOT / "logs" / "arm" / "stop.request"),
    )


def _one_rev(controller: ArmController, axis_name: str) -> None:
    axis = controller.config.axes[axis_name]
    controller.home(axis_name)
    start = controller.positions[axis_name]
    if start is None:
        raise ArmError(f"{axis_name} has no tracked position after homing")
    target = start + axis.units_per_rev
    if target > axis.max_pos:
        target = start - axis.units_per_rev
    controller.move_joint(axis_name, target)
    print(
        f"{axis_name}: homed, then commanded one revolution "
        f"({axis.units_per_rev:g} units); the mark should return to the same spot"
    )


def _limits(config) -> int:
    failures = 0
    for name in AXIS_NAMES:
        axis = config.axes[name]
        for label, value in (
            ("min", axis.min_pos),
            ("max", axis.max_pos),
        ):
            manual_move(config, name, value)
            print(f"{name} {label} {value:g}: accepted")
        try:
            manual_move(config, name, axis.min_pos - 1.0)
        except OutOfRangeError:
            print(f"{name} below min: refused")
        else:
            print(f"{name} below min: ERROR accepted")
            failures += 1
        try:
            manual_move(config, name, axis.max_pos + 1.0)
        except OutOfRangeError:
            print(f"{name} above max: refused")
        else:
            print(f"{name} above max: ERROR accepted")
            failures += 1
    return failures


def run(args: argparse.Namespace, controller: ArmController) -> int:
    if args.command == "stop":
        controller.request_stop()
        print("controlled stop requested")
        return 0

    if args.command == "resume":
        controller.clear_stop()
        print("controlled stop cleared")
        return 0

    if args.command == "estop":
        controller.stop()
        print("emergency stop sent")
        return 0

    if args.command == "limits":
        failures = _limits(controller.config)
        return 1 if failures else 0

    controller.connect()

    if getattr(args, "home_first", False):
        controller.home_all()

    if args.command == "status":
        print(json.dumps(controller.status(), indent=2, sort_keys=True))
    elif args.command == "endstops":
        print(json.dumps(controller.endstops(), indent=2, sort_keys=True))
    elif args.command == "home":
        if args.axis == "all":
            controller.home_all()
        else:
            controller.home(args.axis)
        print("homed")
    elif args.command == "jog":
        controller.jog(args.axis, args.delta)
        print("jogged")
    elif args.command == "one-rev":
        _one_rev(controller, args.axis)
    elif args.command == "tmc":
        print(json.dumps(controller.tmc_dump(args.axis), indent=2))
    else:
        raise ArmError(f"unknown command {args.command!r}")
    return 0


def main(
    argv: list[str] | None = None,
    controller_factory=build_controller,
) -> int:
    args = build_parser().parse_args(argv)
    controller = None
    try:
        controller = controller_factory(args)
        return run(args, controller)
    except ArmError as exc:
        print(f"bench: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("bench: interrupted", file=sys.stderr)
        return 130
    finally:
        audit = getattr(controller, "audit", None)
        if audit is not None and hasattr(audit, "close"):
            audit.close()


if __name__ == "__main__":
    raise SystemExit(main())
