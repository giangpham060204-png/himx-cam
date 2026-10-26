"""TOML configuration for the 3-axis arm.

Pure standard library. No hardware imports.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

AXIS_NAMES = ("m1", "m2", "m3")
LINEAR = "linear"
ROTARY = "rotary"
_PIN_RE = re.compile(r"^gpio([0-9]|1[0-9]|2[0-9])$")
_RESERVED_PINS = {23, 24, 25, 29}
_MISSING = object()


@dataclass(frozen=True)
class AxisConfig:
    name: str
    kind: str
    full_steps_per_rev: int
    microsteps: int
    gear_ratio: float
    lead_mm_per_rev: float | None
    invert_dir: bool
    min_pos: float
    max_pos: float
    max_speed: float
    max_accel: float
    home_direction: int
    home_position: float
    home_travel: float
    home_speed: float
    step_pin: str
    dir_pin: str
    enable_pin: str
    enable_active_low: bool
    endstop_pin: str
    endstop_pullup: bool
    endstop_inverted: bool
    uart_pin: str
    tx_pin: str
    uart_address: int
    run_current: float

    @property
    def units_per_rev(self) -> float:
        if self.kind == ROTARY:
            return 360.0
        if self.lead_mm_per_rev is None:
            raise ConfigError(f"axis {self.name} has no lead_mm_per_rev")
        return self.lead_mm_per_rev

    @property
    def steps_per_unit(self) -> float:
        return (
            self.full_steps_per_rev
            * self.microsteps
            * self.gear_ratio
            / self.units_per_rev
        )

    @property
    def klipper_min(self) -> float:
        return self.min_pos - self.home_travel - 1.0

    @property
    def klipper_max(self) -> float:
        return self.max_pos + self.home_travel + 1.0

    def home_target(self) -> float:
        if self.home_direction < 0:
            return self.min_pos - self.home_travel
        return self.max_pos + self.home_travel


@dataclass(frozen=True)
class RobotConfig:
    stack: str
    r0_mm: float
    hub_height_mm: float
    m1_y_offset_mm: float
    arm_length_mm: float
    m2_cable_wind_deg: float
    slip_ring: bool
    home_order: tuple[str, str, str]
    serial: str
    axes: dict[str, AxisConfig]

    @property
    def config_hash(self) -> str:
        payload = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()[:16]


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing [{name}] section")
    return value


def _require(section: dict[str, Any], key: str, section_name: str) -> Any:
    if key not in section:
        raise ConfigError(f"[{section_name}] is missing {key}")
    return section[key]


def _number(
    section: dict[str, Any],
    key: str,
    section_name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    value = _require(section, key, section_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[{section_name}] {key} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"[{section_name}] {key} must be finite")
    if positive and number <= 0:
        raise ConfigError(f"[{section_name}] {key} must be > 0")
    if nonnegative and number < 0:
        raise ConfigError(f"[{section_name}] {key} must be >= 0")
    return number


def _integer(section: dict[str, Any], key: str, section_name: str) -> int:
    value = _require(section, key, section_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[{section_name}] {key} must be an integer")
    return value


def _boolean(
    section: dict[str, Any],
    key: str,
    section_name: str,
    default: object = _MISSING,
) -> bool:
    if key not in section:
        if default is _MISSING:
            raise ConfigError(f"[{section_name}] is missing {key}")
        return bool(default)
    value = section[key]
    if not isinstance(value, bool):
        raise ConfigError(f"[{section_name}] {key} must be true or false")
    return value


def _string(section: dict[str, Any], key: str, section_name: str) -> str:
    value = _require(section, key, section_name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{section_name}] {key} must be a non-empty string")
    return value.strip()


def _pin(section: dict[str, Any], key: str, section_name: str) -> str:
    value = _string(section, key, section_name)
    match = _PIN_RE.match(value)
    if match is None:
        raise ConfigError(
            f"[{section_name}] {key} must be gpio0..gpio29, got {value!r}"
        )
    number = int(match.group(1))
    if number in _RESERVED_PINS:
        raise ConfigError(
            f"[{section_name}] {key} uses reserved Pico pin gpio{number}"
        )
    return value


def _axis(name: str, raw: object) -> AxisConfig:
    section_name = f"axis.{name}"
    if not isinstance(raw, dict):
        raise ConfigError(f"missing [axis.{name}] section")

    kind = _string(raw, "kind", section_name)
    if kind not in {LINEAR, ROTARY}:
        raise ConfigError(f"[{section_name}] kind must be linear or rotary")

    full_steps = _integer(raw, "full_steps_per_rev", section_name)
    if full_steps < 1:
        raise ConfigError(f"[{section_name}] full_steps_per_rev must be >= 1")

    microsteps = _integer(raw, "microsteps", section_name)
    if microsteps not in {1, 2, 4, 8, 16, 32, 64, 128, 256}:
        raise ConfigError(f"[{section_name}] microsteps must be a power of two in 1..256")

    gear_ratio = _number(raw, "gear_ratio", section_name, positive=True)
    lead = None
    if kind == LINEAR:
        lead = _number(raw, "lead_mm_per_rev", section_name, positive=True)

    min_pos = _number(raw, "min_pos", section_name)
    max_pos = _number(raw, "max_pos", section_name)
    if min_pos >= max_pos:
        raise ConfigError(f"[{section_name}] min_pos must be < max_pos")

    max_speed = _number(raw, "max_speed", section_name, positive=True)
    max_accel = _number(raw, "max_accel", section_name, positive=True)

    home_direction = _integer(raw, "home_direction", section_name)
    if home_direction not in (-1, 1):
        raise ConfigError(f"[{section_name}] home_direction must be -1 or 1")

    home_position = _number(raw, "home_position", section_name)
    if not (min_pos <= home_position <= max_pos):
        raise ConfigError(f"[{section_name}] home_position must be inside the soft limits")

    home_travel = _number(raw, "home_travel", section_name, nonnegative=True)
    if kind == LINEAR and home_travel <= 0:
        raise ConfigError(f"[{section_name}] home_travel must be > 0 for a linear axis")
    home_speed = _number(raw, "home_speed", section_name, positive=True)
    if home_speed > max_speed:
        raise ConfigError(f"[{section_name}] home_speed must be <= max_speed")

    run_current = _number(raw, "run_current", section_name, positive=True)
    if run_current > 1.3:
        raise ConfigError(f"[{section_name}] run_current must be <= 1.3 A")

    uart_address = _integer(raw, "uart_address", section_name)
    if not 0 <= uart_address <= 3:
        raise ConfigError(f"[{section_name}] uart_address must be 0..3")

    return AxisConfig(
        name=name,
        kind=kind,
        full_steps_per_rev=full_steps,
        microsteps=microsteps,
        gear_ratio=gear_ratio,
        lead_mm_per_rev=lead,
        invert_dir=_boolean(raw, "invert_dir", section_name),
        min_pos=min_pos,
        max_pos=max_pos,
        max_speed=max_speed,
        max_accel=max_accel,
        home_direction=home_direction,
        home_position=home_position,
        home_travel=home_travel,
        home_speed=home_speed,
        step_pin=_pin(raw, "step_pin", section_name),
        dir_pin=_pin(raw, "dir_pin", section_name),
        enable_pin=_pin(raw, "enable_pin", section_name),
        enable_active_low=_boolean(raw, "enable_active_low", section_name),
        endstop_pin=_pin(raw, "endstop_pin", section_name),
        endstop_pullup=_boolean(raw, "endstop_pullup", section_name),
        endstop_inverted=_boolean(raw, "endstop_inverted", section_name),
        uart_pin=_pin(raw, "uart_pin", section_name),
        tx_pin=_pin(raw, "tx_pin", section_name),
        uart_address=uart_address,
        run_current=run_current,
    )


def load_config(path: str | Path) -> RobotConfig:
    path = Path(path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from None

    robot = _section(data, "robot")
    klipper = _section(data, "klipper")
    axes_raw = _section(data, "axis")

    if set(axes_raw) != set(AXIS_NAMES):
        raise ConfigError(f"[axis] must define exactly {', '.join(AXIS_NAMES)}")

    stack = _string(robot, "stack", "robot").upper()
    if stack not in {"A", "B"}:
        raise ConfigError("[robot] stack must be A or B")

    home_order_raw = robot.get("home_order", ["m3", "m2", "m1"])
    if (
        not isinstance(home_order_raw, list)
        or len(home_order_raw) != 3
        or not all(isinstance(item, str) for item in home_order_raw)
        or set(home_order_raw) != set(AXIS_NAMES)
    ):
        raise ConfigError("[robot] home_order must be a permutation of m1, m2, m3")

    axes = {name: _axis(name, axes_raw.get(name)) for name in AXIS_NAMES}
    if axes["m1"].kind != LINEAR or axes["m2"].kind != ROTARY or axes["m3"].kind != LINEAR:
        raise ConfigError("axis kinds must be m1=linear, m2=rotary, m3=linear")

    wind = _number(robot, "m2_cable_wind_deg", "robot", positive=True)
    slip_ring = _boolean(robot, "slip_ring", "robot", default=False)
    m2 = axes["m2"]
    if not slip_ring and (
        m2.min_pos < -wind - 1e-9 or m2.max_pos > wind + 1e-9
    ):
        raise ConfigError(
            "m2 soft limits exceed m2_cable_wind_deg; "
            "set slip_ring = true or reduce the limits"
        )
    if not slip_ring and abs(m2.home_target()) > wind + 1e-9:
        raise ConfigError(
            "m2 home_target exceeds m2_cable_wind_deg; "
            "reduce home_travel or set slip_ring = true"
        )

    seen_pins: dict[str, str] = {}
    for name, axis in axes.items():
        for field in ("step_pin", "dir_pin", "enable_pin", "endstop_pin"):
            pin = getattr(axis, field)
            if pin in seen_pins:
                raise ConfigError(
                    f"pin {pin} is used by both {seen_pins[pin]} and {name}.{field}"
                )
            seen_pins[pin] = f"{name}.{field}"

    seen_addresses: dict[int, str] = {}
    for name, axis in axes.items():
        if axis.uart_address in seen_addresses:
            raise ConfigError(
                f"uart_address {axis.uart_address} is used by both "
                f"{seen_addresses[axis.uart_address]} and {name}"
            )
        seen_addresses[axis.uart_address] = name

    kinematics = klipper.get("kinematics", "none")
    if kinematics != "none":
        raise ConfigError("[klipper] kinematics must be none for this plan")

    return RobotConfig(
        stack=stack,
        r0_mm=_number(robot, "r0_mm", "robot", positive=True),
        hub_height_mm=_number(robot, "hub_height_mm", "robot", positive=True),
        m1_y_offset_mm=_number(robot, "m1_y_offset_mm", "robot"),
        arm_length_mm=_number(robot, "arm_length_mm", "robot", positive=True),
        m2_cable_wind_deg=wind,
        slip_ring=slip_ring,
        home_order=tuple(home_order_raw),
        serial=_string(klipper, "serial", "klipper"),
        axes=axes,
    )
