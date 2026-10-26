"""Stack A/B parity against the effective pair-light.html geometry."""

import dataclasses
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.config import load_config  # noqa: E402
from src.kinematics import JointPose, forward, inverse  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "src" / "config" / "arm.toml")
POSES = json.loads(
    (ROOT / "tests" / "fixtures" / "pair_light_poses.json").read_text()
)


def _config_for(stack: str):
    if stack == "A":
        return dataclasses.replace(
            CONFIG,
            stack="A",
            r0_mm=83.0,
            m1_y_offset_mm=59.0,
            hub_height_mm=140.0,
        )
    return dataclasses.replace(
        CONFIG,
        stack="B",
        arm_length_mm=109.0,
        m1_y_offset_mm=59.0,
        hub_height_mm=140.0,
    )


@pytest.mark.parametrize("pose", POSES)
def test_forward_matches_pair_light(pose):
    config = _config_for(pose["stack"])
    joints = JointPose(m1=pose["m1"], m2=pose["m2"], m3=pose["m3"])

    assert forward(config, joints) == pytest.approx(pose["tip"], abs=0.01)


@pytest.mark.parametrize("pose", POSES)
def test_inverse_round_trip_matches_pair_light(pose):
    config = _config_for(pose["stack"])
    joints = JointPose(m1=pose["m1"], m2=pose["m2"], m3=pose["m3"])

    result = inverse(config, tuple(pose["tip"]), current=joints)

    assert (result.m1, result.m2, result.m3) == pytest.approx(
        (pose["m1"], pose["m2"], pose["m3"]), abs=0.01
    )
