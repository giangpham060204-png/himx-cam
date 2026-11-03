"""Operator CLI tests with a fake controller."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import arm_cli  # noqa: E402


class FakeController:
    def __init__(self):
        self.calls = []

    def connect(self):
        self.calls.append(("connect",))

    def status(self):
        return {"klippy_state": "ready", "homed": {}}

    def home(self, axis):
        self.calls.append(("home", axis))

    def home_all(self):
        self.calls.append(("home_all",))

    def move_joint(self, axis, position, speed=None, accel=None):
        self.calls.append(("move", axis, position, speed, accel))

    def jog(self, axis, delta, speed=None, accel=None):
        self.calls.append(("jog", axis, delta, speed, accel))

    def jog_repeat(self, axis, delta, repeat, interval_s=0.2):
        self.calls.append(("jog_repeat", axis, delta, repeat, interval_s))

    def pose(self):
        return (1.0, 2.0, 3.0)

    def cycle(self, moves):
        self.calls.append(("cycle", moves))

    def stop(self):
        self.calls.append(("stop",))

    def restart(self):
        self.calls.append(("restart",))

    def request_stop(self):
        self.calls.append(("request_stop",))

    def clear_stop(self):
        self.calls.append(("clear_stop",))

    def endstops(self):
        return {"m1": "open"}

    def tmc_dump(self, axis):
        return {"gcode_store": []}


def _run(argv, controller):
    return arm_cli.main(argv, controller_factory=lambda args: controller)


def test_status_connects_and_prints_json(capsys):
    controller = FakeController()

    assert _run(["status"], controller) == 0

    assert controller.calls == [("connect",)]
    assert '"klippy_state": "ready"' in capsys.readouterr().out


def test_home_all(capsys):
    controller = FakeController()

    assert _run(["home", "all"], controller) == 0

    assert controller.calls == [("connect",), ("home_all",)]


def test_jog_repeat(capsys):
    controller = FakeController()

    assert _run(["jog", "m1", "1", "--repeat", "3"], controller) == 0

    assert controller.calls == [
        ("connect",),
        ("jog_repeat", "m1", 1.0, 3, 0.2),
    ]


def test_jog_zero_repeat_returns_1(capsys):
    controller = FakeController()

    assert _run(["jog", "m1", "1", "--repeat", "0"], controller) == 1

    assert controller.calls == [("connect",)]
    assert "repeat" in capsys.readouterr().err.lower()


def test_session_keeps_homed_state(capsys):
    controller = FakeController()
    lines = iter(["home all", "jog m1 1", "quit"])

    assert arm_cli.main(
        ["session"],
        controller_factory=lambda args: controller,
        input_fn=lambda prompt: next(lines),
    ) == 0

    assert controller.calls == [
        ("connect",),
        ("home_all",),
        ("jog", "m1", 1.0, None, None),
    ]


def test_cycle_parses_axis_values(capsys):
    controller = FakeController()

    assert _run(["cycle", "--move", "m1=10", "--move", "m2=-5"], controller) == 0

    assert controller.calls == [
        ("connect",),
        ("cycle", [("m1", 10.0), ("m2", -5.0)]),
    ]


def test_jog_home_first(capsys):
    controller = FakeController()

    assert _run(["jog", "m1", "1", "--home-first"], controller) == 0

    assert controller.calls == [
        ("connect",),
        ("home_all",),
        ("jog", "m1", 1.0, None, None),
    ]


def test_estop_skips_connect(capsys):
    controller = FakeController()

    assert _run(["estop"], controller) == 0

    assert controller.calls == [("stop",)]


def test_bad_cycle_value_returns_1(capsys):
    controller = FakeController()

    assert _run(["cycle", "--move", "m1=wat"], controller) == 1

    assert controller.calls == [("connect",)]
    assert "invalid" in capsys.readouterr().err.lower()


def test_jog_repeat_with_speed_returns_1_without_motion(capsys):
    controller = FakeController()

    assert _run(["jog", "m1", "1", "--repeat", "2", "--speed", "5"], controller) == 1

    assert controller.calls == [("connect",)]
    assert "repeat" in capsys.readouterr().err.lower()


def test_jog_repeat_with_accel_returns_1_without_motion(capsys):
    controller = FakeController()

    assert _run(["jog", "m1", "1", "--repeat", "3", "--accel", "9"], controller) == 1

    assert controller.calls == [("connect",)]


def test_restart_command(capsys):
    controller = FakeController()
    controller.restart = lambda: controller.calls.append(("restart",))

    assert _run(["restart"], controller) == 0

    # Restart must skip connect(): it is the recovery path used while
    # Klipper is in shutdown after an emergency stop.
    assert controller.calls == [("restart",)]


def test_stop_and_resume_skip_connect(capsys):
    controller = FakeController()

    assert _run(["stop"], controller) == 0
    assert _run(["resume"], controller) == 0

    assert controller.calls == [("request_stop",), ("clear_stop",)]


def test_session_stop_and_resume(capsys):
    controller = FakeController()
    lines = iter(["stop", "resume", "quit"])

    assert arm_cli.main(
        ["session"],
        controller_factory=lambda args: controller,
        input_fn=lambda prompt: next(lines),
    ) == 0

    assert controller.calls == [
        ("connect",),
        ("request_stop",),
        ("clear_stop",),
    ]
