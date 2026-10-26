"""End-to-end mock: config -> controller -> Moonraker client -> audit."""

import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from arm_control.config import load_config  # noqa: E402
from arm_control.controller import ArmController  # noqa: E402
from arm_control.moonraker import MoonrakerClient  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None, timeout=5.0):
        self.calls.append((method, path, body))
        if path == "/printer/info":
            return {"result": {"state": "ready"}}
        if path == "/printer/objects/list":
            return {
                "result": {
                    "objects": [
                        "manual_stepper m1",
                        "manual_stepper m2",
                        "manual_stepper m3",
                        "query_endstops",
                    ]
                }
            }
        if path == "/printer/query_endstops/status":
            return {"result": {"m1": "open", "m2": "open", "m3": "open"}}
        if path == "/printer/gcode/script":
            return {"result": "ok"}
        if path == "/printer/emergency_stop":
            return {"result": "ok"}
        raise AssertionError(path)


class RecordingAudit:
    def __init__(self):
        self.entries = []

    def record(self, event, **fields):
        self.entries.append((event, fields))


def test_full_mock_session():
    config = load_config(ROOT / "arm_control" / "config" / "arm.toml")
    transport = FakeTransport()
    audit = RecordingAudit()
    controller = ArmController(config, MoonrakerClient(transport), audit)

    controller.connect()
    controller.home_all()
    controller.move_joint("m1", 10.0)
    controller.jog("m2", 5.0)
    controller.cycle([("m3", 20.0), ("m1", 20.0)])

    assert controller.homed == {"m1": True, "m2": True, "m3": True}
    assert controller.positions["m1"] == 20.0
    assert controller.positions["m2"] == 5.0
    assert controller.positions["m3"] == 20.0
    assert controller.pose() == pytest.approx(
        (
            80.0 * math.cos(math.radians(5.0)),
            20.0,
            140.0 + 80.0 * math.sin(math.radians(5.0)),
        )
    )

    controller.stop()
    assert controller.estopped is True
    assert any(event == "estop" for event, _ in audit.entries)

    scripts = [body["script"] for _, path, body in transport.calls if body]
    assert any("STEPPER=m1 MOVE=10" in script for script in scripts)
    assert any("STEPPER=m2 MOVE=5" in script for script in scripts)
