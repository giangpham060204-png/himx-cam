"""Controller state-machine tests with a fake Moonraker client."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from arm_control.config import load_config  # noqa: E402
from arm_control.controller import ArmController  # noqa: E402
from arm_control.errors import (  # noqa: E402
    ArmError,
    MoonrakerError,
    NotHomedError,
    OutOfRangeError,
    TmcFaultError,
)
from arm_control.kinematics import JointPose  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "arm_control" / "config" / "arm.toml")


class RecordingAudit:
    def __init__(self):
        self.entries = []

    def record(self, event, **fields):
        self.entries.append((event, fields))


class FakeClient:
    def __init__(self, state="ready", objects=None, fail=None):
        self.state = state
        self._objects = objects or {
            "manual_stepper m1",
            "manual_stepper m2",
            "manual_stepper m3",
            "query_endstops",
        }
        self.fail = fail
        self.calls = []
        self.ready_calls = []
        self.store = {"gcode_store": [{"message": "ok"}]}

    def wait_ready(self, timeout=10.0):
        self.ready_calls.append(timeout)

    def klippy_state(self):
        return self.state

    def objects(self):
        return set(self._objects)

    def run_script(self, script, timeout=120.0):
        self.calls.append(("run", script))
        if self.fail is not None:
            raise self.fail

    def emergency_stop(self):
        self.calls.append(("estop",))

    def endstop_status(self):
        return {"m1": "open", "m2": "open", "m3": "open"}

    def gcode_store(self, count=50):
        return self.store


def _controller(client=None, audit=None):
    return ArmController(CONFIG, client or FakeClient(), audit or RecordingAudit())


def test_connect_requires_ready_and_all_objects():
    client = FakeClient(objects={"manual_stepper m1"})
    controller = _controller(client)

    with pytest.raises(MoonrakerError, match="manual_stepper m2"):
        controller.connect()


def test_home_sends_home_then_set_position():
    client = FakeClient()
    controller = _controller(client)

    controller.home("m1")

    assert client.calls == [
        (
            "run",
            "MANUAL_STEPPER STEPPER=m1 MOVE=-20 SPEED=5 ACCEL=50 "
            "STOP_ON_ENDSTOP=home",
        ),
        ("run", "MANUAL_STEPPER STEPPER=m1 SET_POSITION=0"),
    ]
    assert controller.homed["m1"] is True
    assert controller.positions["m1"] == 0.0


def test_move_before_home_is_refused_without_gcode():
    client = FakeClient()
    audit = RecordingAudit()
    controller = _controller(client, audit)

    with pytest.raises(NotHomedError, match="m1"):
        controller.move_joint("m1", 10.0)

    assert client.calls == []
    assert any(event == "refuse" for event, _ in audit.entries)


def test_out_of_range_move_is_refused_without_gcode():
    client = FakeClient()
    audit = RecordingAudit()
    controller = _controller(client, audit)
    controller.home("m1")
    client.calls.clear()
    audit.entries.clear()

    with pytest.raises(OutOfRangeError, match="m1"):
        controller.move_joint("m1", 201.0)

    assert client.calls == []
    assert any(event == "refuse" for event, _ in audit.entries)


def test_move_updates_position_and_audits():
    client = FakeClient()
    audit = RecordingAudit()
    controller = _controller(client, audit)
    controller.home("m1")
    client.calls.clear()

    controller.move_joint("m1", 10.0)

    assert controller.positions["m1"] == 10.0
    assert client.calls == [
        ("run", "MANUAL_STEPPER STEPPER=m1 MOVE=10 SPEED=10 ACCEL=50")
    ]
    assert any(event == "move" for event, _ in audit.entries)


def test_jog_and_jog_repeat(monkeypatch):
    client = FakeClient()
    controller = _controller(client)
    controller.home("m1")
    client.calls.clear()
    monkeypatch.setattr("arm_control.controller.time.sleep", lambda _: None)

    controller.jog("m1", 1.0)
    controller.jog_repeat("m1", 1.0, repeat=2, interval_s=0.1)

    assert controller.positions["m1"] == 3.0
    assert len(client.calls) == 3


def test_home_all_uses_configured_order():
    client = FakeClient()
    controller = _controller(client)

    controller.home_all()

    first_home = client.calls[0][1]
    assert "STEPPER=m3" in first_home
    assert controller.homed == {"m1": True, "m2": True, "m3": True}


def test_cycle_stops_on_first_error():
    client = FakeClient(fail=MoonrakerError("boom"))
    controller = _controller(client)
    controller.homed = {"m1": True, "m2": True, "m3": True}
    controller.positions = {"m1": 0.0, "m2": 0.0, "m3": 0.0}

    with pytest.raises(MoonrakerError, match="boom"):
        controller.cycle([("m1", 1.0), ("m2", 2.0)])

    assert len(client.calls) == 1


def test_stop_latches_and_refuses_motion():
    client = FakeClient()
    controller = _controller(client)
    controller.home("m1")
    client.calls.clear()

    controller.stop()

    assert controller.estopped is True
    assert client.calls == [("estop",)]
    with pytest.raises(ArmError, match="emergency stop"):
        controller.move_joint("m1", 1.0)


def test_pose_requires_all_axes_homed():
    controller = _controller()

    with pytest.raises(NotHomedError):
        controller.pose()

    controller.homed = {"m1": True, "m2": True, "m3": True}
    controller.positions = {"m1": 10.0, "m2": 0.0, "m3": 40.0}

    assert controller.pose() == pytest.approx((100.0, 10.0, 140.0))


def test_tmc_dump_runs_command_and_reads_store():
    client = FakeClient()
    controller = _controller(client)

    result = controller.tmc_dump("m1")

    assert client.calls == [
        ("run", "DUMP_TMC STEPPER=m1")
    ]
    assert result == {"gcode_store": [{"message": "ok"}]}


def test_connect_resets_host_position():
    client = FakeClient()
    controller = _controller(client)
    controller.homed = {"m1": True, "m2": True, "m3": True}
    controller.positions = {"m1": 1.0, "m2": 2.0, "m3": 3.0}

    controller.connect()

    assert controller.homed == {"m1": False, "m2": False, "m3": False}
    assert controller.positions == {"m1": None, "m2": None, "m3": None}


def test_tmc_dump_unknown_axis_records_refusal():
    audit = RecordingAudit()
    controller = _controller(audit=audit)

    with pytest.raises(OutOfRangeError, match="m4"):
        controller.tmc_dump("m4")

    assert any(event == "refuse" for event, _ in audit.entries)


def test_run_audits_error_on_unexpected_exception():
    audit = RecordingAudit()
    controller = _controller(FakeClient(fail=OSError("boom")), audit)
    controller.homed = {"m1": True, "m2": True, "m3": True}
    controller.positions = {"m1": 0.0, "m2": 0.0, "m3": 0.0}

    with pytest.raises(OSError, match="boom"):
        controller.move_joint("m1", 10.0)

    assert any(event == "error" for event, _ in audit.entries)


def test_jog_repeat_sleeps_between_moves_only(monkeypatch):
    client = FakeClient()
    controller = _controller(client)
    controller.home("m1")
    client.calls.clear()
    sleeps = []
    monkeypatch.setattr(
        "arm_control.controller.time.sleep", lambda seconds: sleeps.append(seconds)
    )

    controller.jog_repeat("m1", 1.0, repeat=3, interval_s=0.1)

    assert controller.positions["m1"] == 3.0
    assert sleeps == [0.1, 0.1]


def test_connect_waits_for_ready_and_clears_fault():
    client = FakeClient()
    controller = _controller(client)
    controller.faulted = True
    controller.fault_reason = "old fault"

    controller.connect()

    assert client.ready_calls == [10.0]
    assert controller.faulted is False
    assert controller.fault_reason == ""


def test_move_refused_when_klippy_not_ready_latches():
    client = FakeClient(state="shutdown")
    controller = _controller(client)
    controller.homed = {"m1": True, "m2": True, "m3": True}
    controller.positions = {"m1": 0.0, "m2": 0.0, "m3": 0.0}

    with pytest.raises(MoonrakerError, match="shutdown"):
        controller.move_joint("m1", 1.0)

    assert controller.faulted is True
    assert "shutdown" in controller.fault_reason
    assert client.calls == []


def test_move_error_latches_and_refuses_next_move():
    client = FakeClient(fail=MoonrakerError("boom"))
    controller = _controller(client)
    controller.homed = {"m1": True, "m2": True, "m3": True}
    controller.positions = {"m1": 0.0, "m2": 0.0, "m3": 0.0}

    with pytest.raises(MoonrakerError, match="boom"):
        controller.move_joint("m1", 1.0)

    assert controller.faulted is True
    assert "boom" in controller.fault_reason
    assert controller.homed == {"m1": False, "m2": False, "m3": False}
    assert controller.positions == {"m1": None, "m2": None, "m3": None}

    with pytest.raises(ArmError, match="faulted"):
        controller.move_joint("m1", 2.0)

    assert len(client.calls) == 1


def test_connect_failure_leaves_fault_latched(monkeypatch):
    client = FakeClient()
    controller = _controller(client)

    def _timeout(timeout=10.0):
        raise MoonrakerError("not ready")

    monkeypatch.setattr(client, "wait_ready", _timeout)

    with pytest.raises(MoonrakerError, match="not ready"):
        controller.connect()

    assert controller.faulted is True
    assert controller.fault_reason == "not ready"


def test_tmc_dump_latches_on_fault():
    client = FakeClient()
    client.store = {"gcode_store": [{"message": "otpw=1"}]}
    controller = _controller(client)

    with pytest.raises(TmcFaultError, match="otpw"):
        controller.tmc_dump("m1")

    assert controller.faulted is True
    assert "otpw" in controller.fault_reason


def test_tmc_dump_clean_store_does_not_fault():
    client = FakeClient()
    client.store = {"gcode_store": [{"message": "otpw=0"}]}
    controller = _controller(client)

    result = controller.tmc_dump("m1")

    assert result == client.store
    assert controller.faulted is False


def test_tmc_dump_detects_uppercase_faults():
    for message in ("OTPW=1", "DRV_ERR: 1"):
        client = FakeClient()
        client.store = {"gcode_store": [{"message": message}]}
        controller = _controller(client)

        with pytest.raises(TmcFaultError):
            controller.tmc_dump("m1")

        assert controller.faulted is True


def test_tmc_dump_transport_failure_latches_and_audits():
    audit = RecordingAudit()
    controller = _controller(FakeClient(fail=MoonrakerError("boom")), audit)

    with pytest.raises(MoonrakerError, match="boom"):
        controller.tmc_dump("m1")

    assert controller.faulted is True
    assert "boom" in controller.fault_reason
    assert any(event == "error" for event, _ in audit.entries)
    error_entries = [f for e, f in audit.entries if e == "error"]
    assert error_entries[0]["axis"] == "m1"
    assert error_entries[0]["command"] == "DUMP_TMC STEPPER=m1"
    assert "boom" in error_entries[0]["error"]
    assert error_entries[0]["config_hash"] == CONFIG.config_hash


def test_jog_unhomed_while_shutdown_latches_shutdown_not_homed():
    client = FakeClient(state="shutdown")
    audit = RecordingAudit()
    controller = _controller(client, audit)
    assert controller.homed["m1"] is False

    with pytest.raises(MoonrakerError, match="shutdown"):
        controller.jog("m1", 1.0)

    assert controller.faulted is True
    assert "shutdown" in controller.fault_reason
    assert client.calls == []
