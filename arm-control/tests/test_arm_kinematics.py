"""Stack A/B kinematics, wrap, and refusal tests."""

import dataclasses
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from arm_control.config import load_config  # noqa: E402
from arm_control.errors import OutOfRangeError, UnreachableError  # noqa: E402
from arm_control.kinematics import (  # noqa: E402
    JointPose,
    check_joint_limits,
    forward,
    inverse,
    normalize_deg,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "arm_control" / "config" / "arm.toml")


def _stack_b():
    return dataclasses.replace(CONFIG, stack="B", arm_length_mm=100.0)


def test_stack_a_forward_at_zero_angle():
    tip = forward(CONFIG, JointPose(m1=10.0, m2=0.0, m3=40.0))

    assert tip == pytest.approx((100.0, 10.0, 140.0))


def test_stack_a_forward_at_ninety_degrees():
    tip = forward(CONFIG, JointPose(m1=10.0, m2=90.0, m3=40.0))

    assert tip == pytest.approx((0.0, 10.0, 240.0), abs=1e-9)


def test_stack_a_round_trip():
    pose = JointPose(m1=12.5, m2=37.0, m3=22.0)
    tip = forward(CONFIG, pose)
    result = inverse(CONFIG, tip, current=pose)

    assert (result.m1, result.m2, result.m3) == pytest.approx(
        (12.5, 37.0, 22.0), abs=1e-9
    )


def test_stack_a_inverse_chooses_the_wrapped_angle_closest_to_current():
    current = JointPose(m1=0.0, m2=350.0, m3=40.0)
    tip = forward(CONFIG, JointPose(m1=0.0, m2=-10.0, m3=40.0))
    result = inverse(CONFIG, tip, current=current)

    assert result.m2 == pytest.approx(350.0)


def test_stack_b_forward():
    tip = forward(_stack_b(), JointPose(m1=10.0, m2=0.0, m3=20.0))

    assert tip == pytest.approx((120.0, 10.0, 140.0))


def test_stack_b_inverse_uses_the_only_valid_branch():
    # R=100, H=140, tip=(0,0,140): theta=0 gives x3=-100 (invalid),
    # theta=+/-180 gives x3=100 (valid). normalize_deg maps 180 to -180,
    # so the first in-limits wrap equivalent is m2=-180.0.
    result = inverse(_stack_b(), (0.0, 0.0, 140.0))

    assert (result.m1, result.m2, result.m3) == pytest.approx((0.0, -180.0, 100.0))


def test_stack_b_first_branch_wins_when_both_branches_are_valid():
    # dz=90, R=100 -> base=asin(0.9)~=64.158 deg; x=50 puts both branch
    # extensions (x -/+ R*cos(base)) inside m3 limits [0, 100]. With no
    # current position the base branch must come first, deterministically.
    result = inverse(_stack_b(), (50.0, 0.0, 230.0))

    assert result.m1 == pytest.approx(0.0)
    assert result.m3 == pytest.approx(50.0 - 100.0 * math.sqrt(1.0 - 0.9**2))
    assert result.m2 == pytest.approx(math.degrees(math.asin(0.9)) - 360.0)


def test_unreachable_tip_is_refused():
    with pytest.raises(UnreachableError, match="no solution"):
        inverse(CONFIG, (0.0, 0.0, 400.0))


def test_non_finite_tip_is_refused():
    with pytest.raises(UnreachableError, match="finite"):
        inverse(CONFIG, (float("nan"), 0.0, 140.0))


def test_check_joint_limits_rejects_out_of_range():
    with pytest.raises(OutOfRangeError, match="m3"):
        check_joint_limits(CONFIG, JointPose(m1=0.0, m2=0.0, m3=101.0))


def test_normalize_deg():
    assert normalize_deg(0.0) == pytest.approx(0.0)
    assert normalize_deg(180.0) == pytest.approx(-180.0)
    assert normalize_deg(-180.0) == pytest.approx(-180.0)
    assert normalize_deg(540.0) == pytest.approx(-180.0)
