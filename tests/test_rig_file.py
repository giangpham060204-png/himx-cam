"""The rig file the sandbox publishes for Slicer: projection matrices as JSON.

Slicer must not duplicate rig/optics code: it reads the matrices from this
file to project a user-typed 3D rig point into each camera view. The sandbox
computes and publishes; Slicer consumes. These tests pin the file's shape and
the convention it carries -- the matrices must mean exactly what the
sandbox's own ``ray_for`` means by them.
"""

import json
import pathlib
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")

import camera_sandbox  # noqa: E402
import frame_sources  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402
from targeting.triangulation import ray_for  # noqa: E402

OPTICS = optics_for()

# Rig points in the scene's working volume, near the marker at the origin.
NEAR_THE_MARKER = [(0.0, 0.0, 0.0), (10.0, 5.0, -5.0)]


def a_sandbox(seed=3):
    """A sandbox the way test_camera_sandbox.py builds them."""
    frame = Frame.load(pathlib.Path(__file__).resolve().parents[1] / "frame.json")
    return camera_sandbox.Sandbox(
        optics=OPTICS, geometry=camera_sandbox.RigGeometry(),
        rig=Rig(), frame=frame, seed=seed,
    )


def a_rig_file(box, folder):
    """The published file, parsed back -- tests read this, never live objects."""
    path = camera_sandbox.write_rig_file(box, folder / camera_sandbox.RIG_FILE)
    return path, json.loads(path.read_bytes().decode("utf-8"))


def test_the_rig_file_carries_three_cameras_with_matrices_and_dims(tmp_path):
    box = a_sandbox()
    box.render_all()

    _, payload = a_rig_file(box, tmp_path)

    assert [camera["name"] for camera in payload["cameras"]] == ["c1", "c2", "c3"]
    for entry, camera, frame in zip(payload["cameras"], box.cameras, box.frames):
        matrix = entry["projection"]
        assert np.shape(matrix) == (3, 4)
        assert all(isinstance(value, float) and np.isfinite(value)
                   for row in matrix for value in row)
        assert entry["projection"] == np.asarray(camera.projection, float).tolist(), \
            "the file's matrices and the sandbox's live ones must be bit-identical"
        assert isinstance(entry["width"], int) and entry["width"] > 0
        assert isinstance(entry["height"], int) and entry["height"] > 0
        assert (entry["width"], entry["height"]) == (frame.shape[1], frame.shape[0])


def test_a_filed_matrix_means_what_the_solver_means_by_it(tmp_path):
    """The load-bearing check: project through the file's floats, then the ray
    from ``ray_for`` over the same file data must pass through the point."""
    box = a_sandbox()
    _, payload = a_rig_file(box, tmp_path)

    for entry in payload["cameras"]:
        matrix = np.asarray(entry["projection"], float)
        for point in NEAR_THE_MARKER:
            weighed = matrix @ np.array([*point, 1.0])
            assert np.isfinite(weighed).all() and abs(weighed[2]) > 0.0
            pixel = (weighed[0] / weighed[2], weighed[1] / weighed[2])
            origin, direction = ray_for(entry["projection"], pixel)
            origin = np.asarray(origin, float)
            direction = np.asarray(direction, float)
            here = np.asarray(point, float)
            closest = origin + float(np.dot(here - origin, direction)) * direction
            assert float(np.linalg.norm(here - closest)) < 1e-6


def test_a_point_near_the_marker_lands_inside_every_picture(tmp_path):
    box = a_sandbox()
    _, payload = a_rig_file(box, tmp_path)

    for entry in payload["cameras"]:
        matrix = np.asarray(entry["projection"], float)
        for point in NEAR_THE_MARKER:
            weighed = matrix @ np.array([*point, 1.0])
            u, v = weighed[0] / weighed[2], weighed[1] / weighed[2]
            assert 0.0 <= u <= entry["width"] - 1
            assert 0.0 <= v <= entry["height"] - 1


def test_the_rig_file_is_written_atomically(tmp_path):
    box = a_sandbox()

    path = camera_sandbox.write_rig_file(box, tmp_path / camera_sandbox.RIG_FILE)

    assert list(tmp_path.glob("*.tmp")) == [], "the tmp file must be gone"
    payload = json.loads(path.read_bytes().decode("utf-8"))
    assert [camera["name"] for camera in payload["cameras"]] == ["c1", "c2", "c3"]
    assert isinstance(payload["when"], str) and payload["when"]


def test_write_frames_publishes_the_rig_file_beside_the_manifest(tmp_path):
    code = camera_sandbox.main(["--write-frames", str(tmp_path)])

    assert code == 0
    payload = json.loads((tmp_path / "camera-rig.json").read_text(encoding="utf-8"))
    assert [camera["name"] for camera in payload["cameras"]] == ["c1", "c2", "c3"]
    assert all(np.shape(camera["projection"]) == (3, 4)
               for camera in payload["cameras"])


def test_a_camera_with_no_projection_is_refused_not_guessed(tmp_path):
    box = a_sandbox()
    box.cameras[0] = frame_sources.Camera(
        name="c1", optics=OPTICS, projection=None, source=box.cameras[0].source)

    with pytest.raises(RuntimeError, match="no projection matrix"):
        camera_sandbox.write_rig_file(box, tmp_path / camera_sandbox.RIG_FILE)


def test_a_camera_with_no_frame_dims_is_refused_not_guessed(tmp_path):
    stray = types.SimpleNamespace(
        cameras=[frame_sources.Camera(
            name="c1", optics=types.SimpleNamespace(),
            projection=np.eye(3, 4), source=None)],
        frames=[])

    with pytest.raises(RuntimeError, match="no frame dims"):
        camera_sandbox.write_rig_file(stray, tmp_path / camera_sandbox.RIG_FILE)
