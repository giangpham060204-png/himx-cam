"""Forward and inverse kinematics for Stack A and Stack B.

Pure arithmetic: no hardware, no Moonraker, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import AXIS_NAMES, RobotConfig
from .errors import OutOfRangeError, UnreachableError

_TOL = 1e-9


@dataclass(frozen=True)
class JointPose:
    m1: float
    m2: float
    m3: float

    def as_dict(self) -> dict[str, float]:
        return {"m1": self.m1, "m2": self.m2, "m3": self.m3}


def normalize_deg(angle: float) -> float:
    """Wrap an angle to [-180, 180)."""
    return (angle + 180.0) % 360.0 - 180.0


def forward(config: RobotConfig, joints: JointPose) -> tuple[float, float, float]:
    """Device-frame tip position in mm."""
    theta = math.radians(joints.m2)
    y = joints.m1 + config.m1_y_offset_mm
    if config.stack == "A":
        radius = config.r0_mm + joints.m3
        return (
            radius * math.cos(theta),
            y,
            config.hub_height_mm + radius * math.sin(theta),
        )
    return (
        joints.m3 + config.arm_length_mm * math.cos(theta),
        y,
        config.hub_height_mm + config.arm_length_mm * math.sin(theta),
    )


def inverse(
    config: RobotConfig,
    tip: tuple[float, float, float],
    current: JointPose | None = None,
) -> JointPose:
    """Return the joint pose for a tip, or raise UnreachableError."""
    try:
        x, y, z = (float(value) for value in tip)
    except (TypeError, ValueError):
        raise UnreachableError(f"tip must be three numbers, got {tip!r}") from None

    if not all(math.isfinite(value) for value in (x, y, z)):
        raise UnreachableError(f"tip must be finite, got {tip!r}")

    if config.stack == "A":
        candidates = _stack_a_candidates(config, x, y, z)
    else:
        candidates = _stack_b_candidates(config, x, y, z)

    valid = []
    for candidate in candidates:
        try:
            check_joint_limits(config, candidate)
        except OutOfRangeError:
            continue
        valid.append(candidate)

    if not valid:
        raise UnreachableError(
            f"tip {tip!r} has no solution inside the joint limits"
        )

    if current is None:
        return valid[0]
    return min(valid, key=lambda candidate: _travel(candidate, current))


def check_joint_limits(config: RobotConfig, joints: JointPose) -> None:
    """Raise OutOfRangeError if any joint is outside its soft limits."""
    for name in AXIS_NAMES:
        axis = config.axes[name]
        value = getattr(joints, name)
        if not math.isfinite(value):
            raise OutOfRangeError(f"{name} must be finite, got {value!r}")
        if value < axis.min_pos - _TOL or value > axis.max_pos + _TOL:
            raise OutOfRangeError(
                f"{name}={value:.6f} is outside {axis.min_pos:.6f}..{axis.max_pos:.6f}"
            )


def _stack_a_candidates(
    config: RobotConfig, x: float, y: float, z: float
) -> list[JointPose]:
    dz = z - config.hub_height_mm
    radius = math.hypot(x, dz)
    theta = math.degrees(math.atan2(dz, x))
    extension = radius - config.r0_mm
    m1 = y - config.m1_y_offset_mm
    return [
        JointPose(m1=m1, m2=angle, m3=extension)
        for angle in _angle_candidates(theta, config)
    ]


def _stack_b_candidates(
    config: RobotConfig, x: float, y: float, z: float
) -> list[JointPose]:
    radius = config.arm_length_mm
    dz = z - config.hub_height_mm
    if abs(dz) > radius + _TOL:
        return []

    ratio = max(-1.0, min(1.0, dz / radius))
    base = math.degrees(math.asin(ratio))
    # Ordered, deduped: the base branch comes first so the
    # no-current-position return (valid[0]) is deterministic.
    branches = [base, 180.0 - base]
    if branches[1] == branches[0]:
        branches = branches[:1]
    m1 = y - config.m1_y_offset_mm

    candidates: list[JointPose] = []
    for branch in branches:
        for angle in _angle_candidates(branch, config):
            x3 = x - radius * math.cos(math.radians(angle))
            candidates.append(JointPose(m1=m1, m2=angle, m3=x3))
    return candidates


def _angle_candidates(angle: float, config: RobotConfig) -> list[float]:
    axis = config.axes["m2"]
    base = normalize_deg(angle)
    k_min = math.floor((axis.min_pos - base) / 360.0)
    k_max = math.ceil((axis.max_pos - base) / 360.0)
    return [base + 360.0 * k for k in range(k_min, k_max + 1)]


def _travel(candidate: JointPose, current: JointPose) -> float:
    # Raw M2 difference, not normalized: moving 350 -> -10 is a 360° unwind.
    return (
        abs(candidate.m1 - current.m1)
        + abs(candidate.m2 - current.m2)
        + abs(candidate.m3 - current.m3)
    )
