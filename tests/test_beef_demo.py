"""The beef demo calibration: LLFF poses in, a clearly-marked demo metric out.

The real footage has no checkerboard. These tests pin the conversion, the
camera-row mapping and the synthetic scale/centring without pretending the
result is a measured calibration.
"""

import json
import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting import beef_demo  # noqa: E402


def pose_array(rows=20, focal=2000.0):
    """20 valid camera poses, in the LLFF 3x5 + bounds layout."""
    poses = np.zeros((rows, 17), float)
    for index in range(rows):
        pose = poses[index, :15].reshape(3, 5)
        pose[:3, :3] = np.eye(3)
        pose[:3, 3] = (index * 0.5, 0.0, 5.0 + index * 0.1)
        pose[0, 4] = focal
    return poses


def test_the_filtered_camera_shifts_the_pose_rows():
    assert beef_demo.pose_row_for_camera(0, filtered_camera=4) == 0
    assert beef_demo.pose_row_for_camera(11, filtered_camera=4) == 10
    assert beef_demo.pose_row_for_camera(20, filtered_camera=4) == 19
    assert beef_demo.pose_row_for_camera(20, filtered_camera=None) == 20

    with pytest.raises(beef_demo.BeefDemoError, match="filtered"):
        beef_demo.pose_row_for_camera(4, filtered_camera=4)


def test_the_projection_uses_the_320_resize_and_centre():
    pose = np.zeros((3, 5), float)
    pose[:3, :3] = np.eye(3)
    pose[0, 4] = 1000.0

    projection = beef_demo.projection_from_pose(pose)

    point = projection @ np.array([0.0, 0.0, 1.0, 1.0])
    point = point[:2] / point[2]
    assert point == pytest.approx((160.0, 160.0), abs=1e-9)
    # fx and fy are different because the 320 clips squash 2704x2028.
    assert projection[0, 0] == pytest.approx(1000.0 * 320.0 / 2704.0)
    assert projection[1, 1] == pytest.approx(1000.0 * 320.0 / 2028.0)


def test_the_demo_scale_centres_the_anchor_and_keeps_the_pixels():
    pose = np.zeros((3, 5), float)
    pose[:3, :3] = np.eye(3)
    pose[:3, 3] = (0.0, 0.0, 0.0)
    pose[0, 4] = 1000.0
    projection = beef_demo.projection_from_pose(pose)
    anchor = (0.0, 0.0, 10.0)
    scale = 4.0

    demo = beef_demo.demo_projection(projection, anchor, scale)

    # The anchor is the new origin.
    at_anchor = demo @ np.array([0.0, 0.0, 0.0, 1.0])
    at_anchor = at_anchor[:2] / at_anchor[2]
    expected = projection @ np.array([0.0, 0.0, 10.0, 1.0])
    expected = expected[:2] / expected[2]
    assert at_anchor == pytest.approx(expected)

    # A point one LLFF unit away is `scale` demo millimetres away.
    world = np.array([1.0, 0.0, 10.0, 1.0])
    demo_point = demo @ np.array([4.0, 0.0, 0.0, 1.0])
    world_point = projection @ world
    assert demo_point[:2] / demo_point[2] == pytest.approx(
        world_point[:2] / world_point[2])


def test_the_written_calibration_is_marked_demo_and_carries_its_limit(tmp_path):
    poses_path = tmp_path / "poses_bounds.npy"
    np.save(poses_path, pose_array())
    out = tmp_path / "camera-rig.json"

    payload = beef_demo.write_demo_calibration(
        poses_path, out, anchor=(0.0, 0.0, 7.0), scale_mm_per_unit=3.5,
        max_residual_px=17.0)

    assert payload["demo"] is True
    assert payload["estimated"] is True
    assert payload["scale_mm_per_unit"] == 3.5
    assert payload["max_residual_px"] == 17.0
    assert len(payload["cameras"]) == 3
    assert [camera["name"] for camera in payload["cameras"]] == [
        "cam00", "cam11", "cam20"]
    assert json.loads(out.read_text())["demo"] is True


def test_a_missing_pose_file_is_refused(tmp_path):
    with pytest.raises(beef_demo.BeefDemoError, match="poses"):
        beef_demo.write_demo_calibration(
            tmp_path / "missing.npy", tmp_path / "camera-rig.json")
