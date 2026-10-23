"""Tests for the synthetic ground-truth stream and its solve path.

The mock videos can only prove the pictures travel. These tests use the
synthetic scene, where the marker is at the origin, so the solved point can be
checked against truth instead of accepted because it looks plausible.
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

import camera_sandbox  # noqa: E402
import synthetic_stream  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402

OPTICS = optics_for()


def a_box():
    return camera_sandbox.Sandbox(
        optics=OPTICS,
        geometry=camera_sandbox.RigGeometry(),
        rig=Rig(),
        frame=Frame.load(pathlib.Path(__file__).resolve().parents[1] / "frame.json"),
    )


def marker_pixels(box):
    return [tuple(float(value) for value in
                  camera_sandbox.where_it_lands(camera, synthetic_stream.TRUTH_POINT))
            for camera in box.cameras]


class RecordingLink:
    def __init__(self):
        self.sent = []

    def send(self, device, frame, timestamp):
        self.sent.append((device, np.array(frame, copy=True), timestamp))


def test_the_truth_path_solves_the_marker_at_the_origin():
    box = a_box()

    outcome = box.nominate_manually(marker_pixels(box))

    assert outcome.status == "found"
    assert outcome.error_mm == pytest.approx(0.0, abs=1e-6)
    assert outcome.ras == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert max(outcome.residual_px) == pytest.approx(0.0, abs=1e-6)


def test_the_result_payload_carries_truth_and_residuals():
    box = a_box()
    outcome = box.nominate_manually(marker_pixels(box))

    payload = synthetic_stream.result_payload(box, outcome, frame_index=7)

    assert payload["status"] == "found"
    assert payload["frame"] == 7
    assert payload["truth_mm"] == [0.0, 0.0, 0.0]
    assert payload["error_mm"] == pytest.approx(0.0, abs=1e-6)
    assert payload["rig_point"] == pytest.approx([0.0, 0.0, 0.0], abs=1e-6)
    assert max(payload["residual_px"]) == pytest.approx(0.0, abs=1e-6)


def test_a_tick_sends_one_frame_per_camera(tmp_path):
    box = a_box()
    link = RecordingLink()
    stream = synthetic_stream.SyntheticStream(box, link, fps=0, logs_dir=tmp_path)

    assert stream.tick() == 0

    assert [device for device, _, _ in link.sent] == ["c1", "c2", "c3"]
    assert all(frame.shape == (320, 320) for _, frame, _ in link.sent)
    assert [timestamp for _, _, timestamp in link.sent] == [0, 0, 0]


def test_a_complete_click_set_writes_a_truth_result_and_camera_log(tmp_path, capsys):
    box = a_box()
    link = RecordingLink()
    stream = synthetic_stream.SyntheticStream(box, link, fps=0, logs_dir=tmp_path)
    stream.tick()
    click_path = tmp_path / "camera-clicks.json"
    click_path.write_text(json.dumps({
        "pixels": [list(pixel) for pixel in marker_pixels(box)],
        "when": "now",
    }), encoding="utf-8")

    outcome = stream.poll_clicks()

    assert outcome is not None and outcome.status == "found"
    result = json.loads((tmp_path / "camera-stream" / "result.json").read_text())
    assert result["status"] == "found"
    assert result["error_mm"] == pytest.approx(0.0, abs=1e-6)
    assert list(tmp_path.glob("camera-targets-*.csv"))
    assert "click set solved" in capsys.readouterr().out


def test_metadata_publishes_the_rig_and_the_truth(tmp_path):
    box = a_box()

    synthetic_stream.write_metadata(box, tmp_path)

    rig = json.loads((tmp_path / "camera-rig.json").read_text())
    truth = json.loads((tmp_path / "truth.json").read_text())
    assert [camera["name"] for camera in rig["cameras"]] == ["c1", "c2", "c3"]
    assert truth["point_mm"] == [0.0, 0.0, 0.0]
    assert truth["source"] == "synthetic"
