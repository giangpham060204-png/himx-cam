"""Tests for the seam: where the pictures come from.

The load-bearing claim is that nothing above this file may care. So the tests
here are about the contract, not the renderer: the same scene arriving from the
renderer and from storage must be indistinguishable to a caller, and a camera
that is not there must say so rather than return something plausible.
"""

import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")

import frame_sources  # noqa: E402
import synthetic_camera as cam  # noqa: E402
from targeting.optics import optics_for  # noqa: E402

OPTICS = optics_for()


def a_scene(spread=150.0, standoff=500.0):
    K, poses, projections = cam.scene(OPTICS, spread, standoff)
    return K, poses, projections


def test_the_synthetic_source_hands_over_a_frame_of_the_right_kind():
    K, poses, _ = a_scene()
    position, R, t = poses[0]
    source = frame_sources.SyntheticSource(OPTICS, (R, t), np.random.default_rng(1))

    frame = source.grab()

    assert frame.shape == (OPTICS.pixels_y, OPTICS.pixels_x)
    assert frame.dtype == np.uint8


def test_the_synthetic_source_draws_the_marker_where_the_optics_say():
    K, poses, _ = a_scene()
    _, R, t = poses[0]
    source = frame_sources.SyntheticSource(OPTICS, (R, t), np.random.default_rng(1))

    corners = cam.detect_marker(source.grab(), cam.detector())
    found = cam.diagonal_crossing(corners)

    assert np.allclose(found, cam.project(K, R, t, np.zeros((1, 3)))[0], atol=3.0)


def test_stored_frames_replay_what_was_put_in():
    K, poses, _ = a_scene()
    _, R, t = poses[0]
    rendering = frame_sources.SyntheticSource(OPTICS, (R, t), np.random.default_rng(1))
    frames = [rendering.grab() for _ in range(3)]

    replay = frame_sources.StoredFrames(frames)

    assert all(np.array_equal(replay.grab(), frame) for frame in frames)


def test_stored_frames_say_so_when_they_run_out():
    replay = frame_sources.StoredFrames([np.zeros((4, 4), np.uint8)])

    replay.grab()
    with pytest.raises(RuntimeError, match="ran out"):
        replay.grab()


def test_a_video_file_source_reads_back_what_was_written(tmp_path):
    """A phone clip has to be usable, so this reads a real file, not a mock."""
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0,
                             (OPTICS.pixels_x, OPTICS.pixels_y), isColor=False)
    if not writer.isOpened():
        pytest.skip("this OpenCV build has no MJPG encoder")
    for value in (30, 200):
        writer.write(np.full((OPTICS.pixels_y, OPTICS.pixels_x), value, np.uint8))
    writer.release()

    source = frame_sources.VideoFileSource(path, OPTICS)

    assert source.grab().mean() == pytest.approx(30, abs=3)
    assert source.grab().mean() == pytest.approx(200, abs=3)


def test_a_missing_video_is_refused_by_name(tmp_path):
    with pytest.raises(RuntimeError, match="clip.avi"):
        frame_sources.VideoFileSource(tmp_path / "clip.avi", OPTICS)


def test_a_camera_that_is_not_there_says_so():
    source = frame_sources.CameraSource("/dev/definitely-not-a-camera", OPTICS)

    with pytest.raises(RuntimeError, match="no camera"):
        source.grab()


def test_a_camera_record_carries_the_calibration_with_the_frames():
    K, poses, projections = a_scene()
    _, R, t = poses[0]
    record = frame_sources.Camera(
        name="c1", optics=OPTICS, projection=projections[0],
        source=frame_sources.SyntheticSource(OPTICS, (R, t), np.random.default_rng(1)),
    )

    assert record.projection.shape == (3, 4)
    assert record.source.grab().shape == (OPTICS.pixels_y, OPTICS.pixels_x)
