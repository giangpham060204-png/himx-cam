"""Host-side arm controller: homed state, position tracking, safe moves."""

from __future__ import annotations

import time
from typing import Iterable

from .audit import Audit, NullAudit
from .commands import manual_home, manual_move, set_position
from .config import AXIS_NAMES, RobotConfig
from .errors import (
    ArmError,
    MoonrakerError,
    NotHomedError,
    OutOfRangeError,
)
from .kinematics import JointPose, forward
from .moonraker import MoonrakerClient


class ArmController:
    def __init__(
        self,
        config: RobotConfig,
        client: MoonrakerClient,
        audit: Audit | None = None,
    ):
        self.config = config
        self.client = client
        self.audit = audit or NullAudit()
        self.homed = {name: False for name in AXIS_NAMES}
        self.positions: dict[str, float | None] = {
            name: None for name in AXIS_NAMES
        }
        self.estopped = False
        self.faulted = False
        self.fault_reason = ""

    def connect(self, wait_timeout: float = 10.0) -> None:
        try:
            self.client.wait_ready(timeout=wait_timeout)
            state = self.client.klippy_state()
            if state != "ready":
                raise MoonrakerError(f"Klipper is {state!r}, not ready")
            objects = self.client.objects()
            required = {f"manual_stepper {name}" for name in AXIS_NAMES}
            required.add("query_endstops")
            missing = required - objects
            if missing:
                raise MoonrakerError(
                    "Moonraker is missing objects: "
                    + ", ".join(sorted(missing))
                )
        except Exception as exc:
            self._fault(str(exc))
            raise

        self.homed = {name: False for name in AXIS_NAMES}
        self.positions = {name: None for name in AXIS_NAMES}
        self.estopped = False
        self.faulted = False
        self.fault_reason = ""
        self.audit.record(
            "connect", klippy_state=state, config_hash=self.config.config_hash
        )

    def status(self) -> dict[str, object]:
        tip = None
        if all(self.homed.values()):
            tip = self.pose()
        return {
            "klippy_state": self.client.klippy_state(),
            "homed": dict(self.homed),
            "positions": dict(self.positions),
            "tip": tip,
            "estopped": self.estopped,
            "faulted": self.faulted,
            "fault_reason": self.fault_reason,
            "config_hash": self.config.config_hash,
        }

    def home(self, axis_name: str) -> None:
        try:
            self._require_axis(axis_name)
            self._require_operable()
            self._require_ready()
            axis = self.config.axes[axis_name]
            command = manual_home(self.config, axis_name)
            zero = set_position(self.config, axis_name, axis.home_position)
        except ArmError as exc:
            self._record_refusal(axis_name, exc)
            raise
        self._run("home", axis_name, command)
        self._run("set_position", axis_name, zero)
        self.positions[axis_name] = axis.home_position
        self.homed[axis_name] = True

    def home_all(self) -> None:
        for axis_name in self.config.home_order:
            self.home(axis_name)

    def move_joint(
        self,
        axis_name: str,
        position: float,
        speed: float | None = None,
        accel: float | None = None,
    ) -> None:
        try:
            self._require_axis(axis_name)
            self._require_operable()
            self._require_ready()
            self._require_homed(axis_name)
            command = manual_move(self.config, axis_name, position, speed, accel)
        except ArmError as exc:
            self._record_refusal(axis_name, exc)
            raise
        self._run("move", axis_name, command)
        self.positions[axis_name] = position

    def jog(
        self,
        axis_name: str,
        delta: float,
        speed: float | None = None,
        accel: float | None = None,
    ) -> None:
        try:
            self._require_axis(axis_name)
            self._require_operable()
            current = self.positions[axis_name]
            if current is None or not self.homed[axis_name]:
                raise NotHomedError(f"axis {axis_name} is not homed")
            target = current + delta
        except ArmError as exc:
            self._record_refusal(axis_name, exc)
            raise
        self.move_joint(axis_name, target, speed, accel)

    def jog_repeat(
        self,
        axis_name: str,
        delta: float,
        repeat: int,
        interval_s: float = 0.2,
    ) -> None:
        if repeat < 1:
            error = OutOfRangeError("repeat must be >= 1")
            self._record_refusal(axis_name, error)
            raise error
        for index in range(repeat):
            if index:
                time.sleep(interval_s)
            self.jog(axis_name, delta)

    def cycle(self, moves: Iterable[tuple[str, float]]) -> None:
        for axis_name, position in moves:
            self.move_joint(axis_name, position)

    def pose(self) -> tuple[float, float, float]:
        if not all(self.homed.values()):
            missing = [name for name, homed in self.homed.items() if not homed]
            raise NotHomedError(
                "pose requires all axes homed; missing " + ", ".join(missing)
            )
        positions = self.positions
        joints = JointPose(
            m1=float(positions["m1"]),
            m2=float(positions["m2"]),
            m3=float(positions["m3"]),
        )
        return forward(self.config, joints)

    def stop(self) -> None:
        try:
            self.client.emergency_stop()
        finally:
            self.estopped = True
            self.homed = {name: False for name in AXIS_NAMES}
            self.positions = {name: None for name in AXIS_NAMES}
            self.audit.record("estop", config_hash=self.config.config_hash)

    def endstops(self) -> dict[str, str]:
        return self.client.endstop_status()

    def tmc_dump(self, axis_name: str) -> object:
        try:
            self._require_axis(axis_name)
        except ArmError as exc:
            self._record_refusal(axis_name, exc)
            raise
        self.client.run_script(f"DUMP_TMC STEPPER={axis_name}")
        return self.client.gcode_store(count=20)

    def _fault(self, reason: str) -> None:
        self.faulted = True
        self.fault_reason = reason
        self.homed = {name: False for name in AXIS_NAMES}
        self.positions = {name: None for name in AXIS_NAMES}
        self.audit.record(
            "fault", reason=reason, config_hash=self.config.config_hash
        )

    def _require_operable(self) -> None:
        if self.estopped:
            raise ArmError(
                "emergency stop is latched; restart Klipper and reconnect"
            )
        if self.faulted:
            raise ArmError(
                f"controller faulted: {self.fault_reason}; "
                "restart Klipper and reconnect"
            )

    def _require_ready(self) -> None:
        try:
            state = self.client.klippy_state()
        except Exception as exc:
            self._fault(str(exc))
            raise
        if state != "ready":
            reason = f"Klipper is {state!r}, not ready"
            self._fault(reason)
            raise MoonrakerError(reason)

    def _record_refusal(self, axis_name: str, error: ArmError) -> None:
        self.audit.record(
            "refuse",
            axis=axis_name,
            error=str(error),
            config_hash=self.config.config_hash,
        )

    def _run(self, event: str, axis_name: str, command: str) -> None:
        try:
            self.client.run_script(command)
        except Exception as exc:
            self._fault(str(exc))
            self.audit.record(
                "error",
                axis=axis_name,
                command=command,
                error=str(exc),
                config_hash=self.config.config_hash,
            )
            raise
        self.audit.record(
            event,
            axis=axis_name,
            command=command,
            config_hash=self.config.config_hash,
        )

    def _require_axis(self, axis_name: str) -> None:
        if axis_name not in AXIS_NAMES:
            raise OutOfRangeError(
                f"unknown axis {axis_name!r}; expected m1, m2, or m3"
            )

    def _require_homed(self, axis_name: str) -> None:
        if not self.homed[axis_name]:
            raise NotHomedError(f"axis {axis_name} is not homed")
