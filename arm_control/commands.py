"""Build MANUAL_STEPPER commands and enforce host-side limits."""

from __future__ import annotations

import math

from .config import RobotConfig
from .errors import OutOfRangeError


def format_number(value: float) -> str:
    if not math.isfinite(value):
        raise OutOfRangeError(f"value must be finite, got {value!r}")
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _axis(config: RobotConfig, axis_name: str):
    try:
        return config.axes[axis_name]
    except KeyError:
        raise OutOfRangeError(
            f"unknown axis {axis_name!r}; expected m1, m2, or m3"
        ) from None


def _check_soft_limit(config: RobotConfig, axis_name: str, position: float) -> None:
    axis = _axis(config, axis_name)
    if not math.isfinite(position):
        raise OutOfRangeError(f"{axis_name} position must be finite")
    if position < axis.min_pos or position > axis.max_pos:
        raise OutOfRangeError(
            f"{axis_name}={position:.6f} is outside "
            f"{axis.min_pos:.6f}..{axis.max_pos:.6f}"
        )


def _check_speed(config: RobotConfig, axis_name: str, speed: float, accel: float) -> None:
    axis = _axis(config, axis_name)
    if not math.isfinite(speed) or speed <= 0:
        raise OutOfRangeError(f"{axis_name} speed must be > 0")
    if speed > axis.max_speed:
        raise OutOfRangeError(
            f"{axis_name} speed {speed:g} exceeds {axis.max_speed:g}"
        )
    if not math.isfinite(accel) or accel <= 0:
        raise OutOfRangeError(f"{axis_name} accel must be > 0")
    if accel > axis.max_accel:
        raise OutOfRangeError(
            f"{axis_name} accel {accel:g} exceeds {axis.max_accel:g}"
        )


def manual_move(
    config: RobotConfig,
    axis_name: str,
    position: float,
    speed: float | None = None,
    accel: float | None = None,
) -> str:
    axis = _axis(config, axis_name)
    _check_soft_limit(config, axis_name, position)
    speed = axis.max_speed if speed is None else speed
    accel = axis.max_accel if accel is None else accel
    _check_speed(config, axis_name, speed, accel)
    return (
        f"MANUAL_STEPPER STEPPER={axis_name} "
        f"MOVE={format_number(position)} "
        f"SPEED={format_number(speed)} ACCEL={format_number(accel)}"
    )


def manual_home(
    config: RobotConfig,
    axis_name: str,
    speed: float | None = None,
    accel: float | None = None,
) -> str:
    axis = _axis(config, axis_name)
    target = axis.home_target()
    if not (axis.klipper_min <= target <= axis.klipper_max):
        raise OutOfRangeError(
            f"{axis_name} home target {target:g} is outside Klipper's "
            f"{axis.klipper_min:g}..{axis.klipper_max:g}"
        )
    speed = axis.home_speed if speed is None else speed
    accel = axis.max_accel if accel is None else accel
    _check_speed(config, axis_name, speed, accel)
    check = "inverted_home" if axis.endstop_inverted else "home"
    return (
        f"MANUAL_STEPPER STEPPER={axis_name} "
        f"MOVE={format_number(target)} "
        f"SPEED={format_number(speed)} ACCEL={format_number(accel)} "
        f"STOP_ON_ENDSTOP={check}"
    )


def set_position(config: RobotConfig, axis_name: str, position: float) -> str:
    _check_soft_limit(config, axis_name, position)
    return (
        f"MANUAL_STEPPER STEPPER={axis_name} "
        f"SET_POSITION={format_number(position)}"
    )
