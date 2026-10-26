"""Command strings and host-side safety checks."""

import dataclasses
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.commands import (  # noqa: E402
    format_number,
    manual_home,
    manual_move,
    set_position,
)
from src.config import load_config  # noqa: E402
from src.errors import OutOfRangeError  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "src" / "config" / "arm.toml")


def test_manual_move_uses_default_speed_and_accel():
    assert manual_move(CONFIG, "m1", 10.0) == (
        "MANUAL_STEPPER STEPPER=m1 MOVE=10 SPEED=10 ACCEL=50"
    )


def test_manual_move_uses_rotary_defaults():
    assert manual_move(CONFIG, "m2", 10.0) == (
        "MANUAL_STEPPER STEPPER=m2 MOVE=10 SPEED=30 ACCEL=180"
    )


def test_manual_move_accepts_explicit_speed_and_accel():
    assert manual_move(CONFIG, "m1", 1.5, speed=2.0, accel=10.0) == (
        "MANUAL_STEPPER STEPPER=m1 MOVE=1.5 SPEED=2 ACCEL=10"
    )


def test_manual_move_refuses_out_of_range():
    with pytest.raises(OutOfRangeError, match="m1"):
        manual_move(CONFIG, "m1", -0.1)


def test_manual_move_refuses_speed_above_cap():
    with pytest.raises(OutOfRangeError, match="speed"):
        manual_move(CONFIG, "m1", 1.0, speed=11.0)


def test_manual_home_uses_home_target():
    assert manual_home(CONFIG, "m1") == (
        "MANUAL_STEPPER STEPPER=m1 MOVE=-20 SPEED=5 ACCEL=50 "
        "STOP_ON_ENDSTOP=home"
    )


def test_manual_home_honours_inverted_endstop():
    inverted = dataclasses.replace(CONFIG.axes["m1"], endstop_inverted=True)
    config = dataclasses.replace(CONFIG, axes={**CONFIG.axes, "m1": inverted})

    assert manual_home(config, "m1").endswith("STOP_ON_ENDSTOP=inverted_home")


def test_set_position_refuses_outside_soft_limits():
    with pytest.raises(OutOfRangeError, match="m2"):
        set_position(CONFIG, "m2", 400.0)


def test_format_number_removes_trailing_zeroes_and_negative_zero():
    assert format_number(10.0) == "10"
    assert format_number(1.5) == "1.5"
    assert format_number(-0.0) == "0"
