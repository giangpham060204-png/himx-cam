#!/usr/bin/env python3
"""Operator CLI for the 3-axis arm."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arm_control.audit import AuditLog  # noqa: E402
from arm_control.config import load_config  # noqa: E402
from arm_control.controller import ArmController  # noqa: E402
from arm_control.errors import ArmError  # noqa: E402
from arm_control.stopflag import StopFlag  # noqa: E402
from arm_control.moonraker import MoonrakerClient  # noqa: E402
from run_context import build_context  # noqa: E402
from transport_factory import build_transport  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual control for the 3-axis arm")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "arm_control" / "config" / "arm.toml",
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
    sub.add_parser("pose")
    sub.add_parser("session")

    home = sub.add_parser("home")
    home.add_argument("axis", choices=["m1", "m2", "m3", "all"])

    jog = sub.add_parser("jog")
    jog.add_argument("axis", choices=["m1", "m2", "m3"])
    jog.add_argument("delta", type=float)
    jog.add_argument("--speed", type=float, default=None)
    jog.add_argument("--accel", type=float, default=None)
    jog.add_argument("--repeat", type=int, default=1)
    jog.add_argument("--interval", type=float, default=0.2)
    jog.add_argument("--home-first", action="store_true")

    move = sub.add_parser("move")
    move.add_argument("axis", choices=["m1", "m2", "m3"])
    move.add_argument("position", type=float)
    move.add_argument("--speed", type=float, default=None)
    move.add_argument("--accel", type=float, default=None)
    move.add_argument("--home-first", action="store_true")

    cycle = sub.add_parser("cycle")
    cycle.add_argument(
        "--move",
        action="append",
        required=True,
        metavar="AXIS=VALUE",
    )
    cycle.add_argument("--home-first", action="store_true")

    tmc = sub.add_parser("tmc")
    tmc.add_argument("axis", choices=["m1", "m2", "m3"])

    sub.add_parser("stop")
    sub.add_parser("resume")
    sub.add_parser("estop")
    sub.add_parser("restart")
    return parser


def build_controller(args: argparse.Namespace) -> ArmController:
    config = load_config(args.config)
    log_path = args.log or (
        ROOT
        / "logs"
        / "arm"
        / f"arm-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    )
    audit = AuditLog(log_path, context=build_context(ROOT, args.operator, args.hardware_serial))
    transport = build_transport(args.url, args.transport, args.timeout)
    return ArmController(
        config,
        MoonrakerClient(transport),
        audit,
        stop_flag=StopFlag(ROOT / "logs" / "arm" / "stop.request"),
    )


def _parse_moves(values: list[str]) -> list[tuple[str, float]]:
    moves = []
    for value in values:
        axis, separator, raw = value.partition("=")
        if not separator or axis not in {"m1", "m2", "m3"}:
            raise ArmError(f"invalid cycle move {value!r}; expected AXIS=VALUE")
        try:
            position = float(raw)
        except ValueError:
            raise ArmError(f"invalid cycle value {raw!r} for {axis}") from None
        moves.append((axis, position))
    return moves


def _session(controller: ArmController, input_fn=input) -> int:
    print("arm session; type help or quit")
    while True:
        try:
            line = input_fn("arm> ")
        except (EOFError, StopIteration):
            break
        words = line.split()
        if not words:
            continue
        command = words[0].lower()
        try:
            if command in {"quit", "exit"}:
                break
            if command == "help":
                print("status | home AXIS|all | jog AXIS DELTA | move AXIS POSITION")
                print("pose | endstops | tmc AXIS | stop | resume | estop | quit")
            elif command == "status":
                print(json.dumps(controller.status(), indent=2, sort_keys=True))
            elif command == "home":
                if len(words) != 2:
                    raise ArmError("usage: home m1|m2|m3|all")
                if words[1] == "all":
                    controller.home_all()
                else:
                    controller.home(words[1])
                print("homed")
            elif command == "jog":
                if len(words) != 3:
                    raise ArmError("usage: jog AXIS DELTA")
                controller.jog(words[1], float(words[2]))
                print("jogged")
            elif command == "move":
                if len(words) != 3:
                    raise ArmError("usage: move AXIS POSITION")
                controller.move_joint(words[1], float(words[2]))
                print("moved")
            elif command == "pose":
                print(json.dumps({"tip": controller.pose()}, indent=2))
            elif command == "endstops":
                print(json.dumps(controller.endstops(), indent=2, sort_keys=True))
            elif command == "tmc":
                if len(words) != 2:
                    raise ArmError("usage: tmc AXIS")
                print(json.dumps(controller.tmc_dump(words[1]), indent=2))
            elif command == "stop":
                controller.request_stop()
                print("controlled stop requested")
            elif command == "resume":
                controller.clear_stop()
                print("controlled stop cleared")
            elif command == "estop":
                controller.stop()
                print("emergency stop sent")
                break
            else:
                raise ArmError(f"unknown session command {command!r}")
        except (ArmError, ValueError) as exc:
            print(f"arm: {exc}", file=sys.stderr)
    return 0


def run(
    args: argparse.Namespace,
    controller: ArmController,
    input_fn=input,
) -> int:
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

    if args.command == "session":
        controller.connect()
        return _session(controller, input_fn)

    controller.connect()

    if getattr(args, "home_first", False):
        controller.home_all()

    if args.command == "status":
        print(json.dumps(controller.status(), indent=2, sort_keys=True))
    elif args.command == "endstops":
        print(json.dumps(controller.endstops(), indent=2, sort_keys=True))
    elif args.command == "pose":
        print(json.dumps({"tip": controller.pose()}, indent=2))
    elif args.command == "home":
        if args.axis == "all":
            controller.home_all()
        else:
            controller.home(args.axis)
        print("homed")
    elif args.command == "jog":
        if args.repeat < 1:
            raise ArmError("--repeat must be >= 1")
        if args.repeat > 1:
            if args.speed is not None or args.accel is not None:
                raise ArmError(
                    "--speed/--accel cannot be combined with --repeat > 1; "
                    "use --repeat 1"
                )
            controller.jog_repeat(
                args.axis, args.delta, args.repeat, args.interval
            )
        else:
            controller.jog(
                args.axis, args.delta, args.speed, args.accel
            )
        print("jogged")
    elif args.command == "move":
        controller.move_joint(
            args.axis, args.position, args.speed, args.accel
        )
        print("moved")
    elif args.command == "cycle":
        controller.cycle(_parse_moves(args.move))
        print("cycle complete")
    elif args.command == "tmc":
        print(json.dumps(controller.tmc_dump(args.axis), indent=2))
    elif args.command == "restart":
        controller.restart()
        print("restarted")
    else:
        raise ArmError(f"unknown command {args.command!r}")
    return 0


def main(
    argv: list[str] | None = None,
    controller_factory=build_controller,
    input_fn=input,
) -> int:
    args = build_parser().parse_args(argv)
    controller = None
    try:
        controller = controller_factory(args)
        return run(args, controller, input_fn)
    except ArmError as exc:
        print(f"arm: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("arm: interrupted", file=sys.stderr)
        return 130
    finally:
        audit = getattr(controller, "audit", None)
        if audit is not None and hasattr(audit, "close"):
            audit.close()


if __name__ == "__main__":
    raise SystemExit(main())
