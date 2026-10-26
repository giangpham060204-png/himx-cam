"""Bench CLI tests with a fake controller."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import arm_bench  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeController:
    def __init__(self):
        from src.config import load_config

        self.config = load_config(ROOT / "src" / "config" / "arm.toml")
        self.homed = {"m1": True, "m2": True, "m3": True}
        self.positions = {"m1": 0.0, "m2": 0.0, "m3": 0.0}
        self.calls = []

    def connect(self):
        self.calls.append(("connect",))

    def status(self):
        return {"klippy_state": "ready"}

    def endstops(self):
        return {"m1": "open"}

    def home(self, axis):
        self.calls.append(("home", axis))

    def home_all(self):
        self.calls.append(("home_all",))

    def jog(self, axis, delta, speed=None, accel=None):
        self.calls.append(("jog", axis, delta))

    def move_joint(self, axis, position, speed=None, accel=None):
        self.calls.append(("move", axis, position))

    def tmc_dump(self, axis):
        self.calls.append(("tmc", axis))
        return {"gcode_store": []}

    def stop(self):
        self.calls.append(("stop",))


def _run(argv, controller):
    return arm_bench.main(argv, controller_factory=lambda args: controller)


def test_one_rev_commands_one_revolution(capsys):
    controller = FakeController()

    assert _run(["one-rev", "m2"], controller) == 0

    assert controller.calls == [
        ("connect",),
        ("home", "m2"),
        ("move", "m2", 360.0),
    ]
    assert "one revolution" in capsys.readouterr().out.lower()


def test_limits_is_a_host_side_check_without_motion(capsys):
    controller = FakeController()

    assert _run(["limits"], controller) == 0

    assert controller.calls == []
    output = capsys.readouterr().out
    assert "m1" in output
    assert "refused" in output


def test_jog_home_first(capsys):
    controller = FakeController()

    assert _run(["jog", "m1", "1", "--home-first"], controller) == 0

    assert controller.calls == [
        ("connect",),
        ("home_all",),
        ("jog", "m1", 1.0),
    ]


def test_tmc_dump(capsys):
    controller = FakeController()

    assert _run(["tmc", "m1"], controller) == 0

    assert controller.calls == [("connect",), ("tmc", "m1")]


def test_estop_skips_connect(capsys):
    controller = FakeController()

    assert _run(["estop"], controller) == 0

    assert controller.calls == [("stop",)]


def test_limits_returns_1_when_out_of_range_accepted(monkeypatch, capsys):
    import argparse

    import arm_bench

    controller = FakeController()
    monkeypatch.setattr(arm_bench, "manual_move", lambda *args, **kwargs: None)

    args = argparse.Namespace(command="limits")
    assert arm_bench.run(args, controller) == 1

    assert "ERROR accepted" in capsys.readouterr().out
