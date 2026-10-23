"""Synthetic videos with a known point: the video path's truth anchor.

The mock footage has no calibration and no truth. These tests build three MP4s
from the same synthetic rig the sandbox uses, then drive the real video sender
over them. The marker centre is known, so ``error_mm`` is a real error rather
than a self-consistency residual.
"""

import json
import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")
pytest.importorskip("matplotlib")

import camera_stream  # noqa: E402
import click_solver  # noqa: E402
import synthetic_camera as cam  # noqa: E402
import synthetic_videos  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402

OPTICS = optics_for()
ROOT = pathlib.Path(__file__).resolve().parents[1]


class RecordingLink:
    def __init__(self):
        self.sent = []

    def send(self, device, frame, timestamp):
        self.sent.append((device, np.array(frame, copy=True), timestamp))


def test_generate_writes_three_videos_a_rig_truth_and_marker_centres(tmp_path):
    result = synthetic_videos.generate(tmp_path, frames=3, fps=10.0)

    for name in camera_stream.VIDEO_NAMES:
        assert (tmp_path / name).is_file()
    calibration = click_solver.load_calibration(result["rig"])
    assert len(calibration.projections) == 3
    assert (calibration.width, calibration.height) == (OPTICS.pixels_x, OPTICS.pixels_y)

    truth = json.loads(pathlib.Path(result["truth"]).read_text())
    assert truth["point_mm"] == [0.0, 0.0, 0.0]
    clicks = json.loads(pathlib.Path(result["clicks"]).read_text())
    assert clicks["frame_id"] == 0
    assert len(clicks["pixels"]) == 3
    assert all(0.0 <= value < OPTICS.pixels_x for pixel in clicks["pixels"]
               for value in pixel)

    detector = cam.detector()
    for name in camera_stream.VIDEO_NAMES:
        capture = cv2.VideoCapture(str(tmp_path / name))
        try:
            ok, frame = capture.read()
        finally:
            capture.release()
        assert ok
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        assert gray.shape == (OPTICS.pixels_y, OPTICS.pixels_x)
        cam.detect_marker(gray, detector)


def test_the_video_sender_solves_to_the_known_origin(tmp_path):
    result = synthetic_videos.generate(tmp_path, frames=3, fps=10.0)
    truth = tuple(json.loads(pathlib.Path(result["truth"]).read_text())["point_mm"])
    clicks = json.loads(pathlib.Path(result["clicks"]).read_text())
    calibration = click_solver.load_calibration(result["rig"])
    sources = camera_stream.open_sources(
        camera_stream.video_paths(tmp_path), OPTICS)
    link = RecordingLink()
    logs = tmp_path / "logs"
    frame = Frame.load(ROOT / "frame.json")

    stream = camera_stream.ClickableVideoStream(
        sources, link, fps=0, loop=False, logs_dir=logs,
        calibration=calibration, rig=Rig(), frame=frame, truth=truth)
    stream.tick()
    clicks_path = logs / "camera-clicks.json"
    clicks_path.write_text(json.dumps({
        "pixels": clicks["pixels"],
        "frame_id": 0,
        "when": "now",
    }), encoding="utf-8")

    outcome = stream.poll_clicks()

    assert outcome is not None and outcome.status == "found"
    assert outcome.error_mm is not None and outcome.error_mm < 1.5
    assert outcome.rig_point == pytest.approx((0.0, 0.0, 0.0), abs=1.5)
    payload = json.loads((logs / "camera-stream" / "result.json").read_text())
    assert payload["source"] == "video"
    assert payload["calibrated"] is True
    assert payload["truth_mm"] == [0.0, 0.0, 0.0]
    assert payload["error_mm"] == pytest.approx(outcome.error_mm)
    assert list(logs.glob("camera-targets-*.csv"))


def test_a_demo_calibration_is_not_written_to_the_camera_log(tmp_path):
    from dataclasses import replace

    result = synthetic_videos.generate(tmp_path, frames=3, fps=10.0)
    clicks = json.loads(pathlib.Path(result["clicks"]).read_text())
    calibration = click_solver.load_calibration(result["rig"])
    demo = replace(calibration, demo=True, max_residual_px=50.0)
    sources = camera_stream.open_sources(
        camera_stream.video_paths(tmp_path), OPTICS)
    logs = tmp_path / "logs"
    stream = camera_stream.ClickableVideoStream(
        sources, RecordingLink(), fps=0, loop=False, logs_dir=logs,
        calibration=demo, rig=Rig(), frame=Frame.load(ROOT / "frame.json"))
    stream.tick()
    (logs / "camera-clicks.json").write_text(json.dumps({
        "pixels": clicks["pixels"],
        "frame_id": 0,
        "when": "now",
    }), encoding="utf-8")

    outcome = stream.poll_clicks()

    assert outcome is not None and outcome.status == "found"
    payload = json.loads((logs / "camera-stream" / "result.json").read_text())
    assert payload["demo"] is True
    assert payload["log_error"].startswith("demo calibration")
    assert not list(logs.glob("camera-targets-*.csv"))
