"""Tests for the Slicer camera stream: one axial frame per camera, in step.

The transport is Slicer's problem; this file tests the part that decides what
belongs in a message and when the next frame is due. A fake link records what
would have gone over the wire, so a shape mistake or a lost camera fails here
without a Slicer install.
"""

import json
import pathlib
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")
pytest.importorskip("matplotlib")

import camera_stream  # noqa: E402
import click_solver  # noqa: E402
import synthetic_camera as cam  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402

OPTICS = optics_for()


class FakeSource:
    """A finite source, shaped like frame_sources.FrameSource."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.given = 0

    def grab(self):
        if self.given >= len(self.frames):
            raise RuntimeError("the stored frames ran out")
        frame = self.frames[self.given]
        self.given += 1
        return frame


class RecordingLink:
    """Records (device, frame, timestamp) instead of opening a socket."""

    def __init__(self):
        self.sent = []

    def send(self, device, frame, timestamp):
        self.sent.append((device, np.array(frame, copy=True), timestamp))


def three_sources(frames_per_source=2, size=(4, 5)):
    return [FakeSource([np.full(size, index, np.uint8)
                        for _ in range(frames_per_source)])
            for index in range(3)]


def test_one_tick_sends_one_frame_per_camera_in_order():
    link = RecordingLink()
    stream = camera_stream.CameraStream(three_sources(), link, fps=0)

    assert stream.tick() == 0

    assert [device for device, _, _ in link.sent] == ["c1", "c2", "c3"]
    assert [timestamp for _, _, timestamp in link.sent] == [0, 0, 0]
    assert all(frame.shape == (4, 5) for _, frame, _ in link.sent)


def test_the_next_tick_advances_the_shared_frame_number():
    link = RecordingLink()
    stream = camera_stream.CameraStream(three_sources(), link, fps=0)

    stream.tick()
    stream.tick()

    assert [timestamp for _, _, timestamp in link.sent] == [0, 0, 0, 1, 1, 1]


def test_a_looping_stream_restarts_every_source_together():
    calls = []

    def factory():
        calls.append(True)
        return three_sources(frames_per_source=2)

    link = RecordingLink()
    stream = camera_stream.CameraStream(
        factory(), link, fps=0, loop=True, source_factory=factory)

    assert stream.run(max_frames=4) == 4
    assert len(link.sent) == 12
    assert len(calls) == 2, "one initial source set plus one rewind"


def test_a_non_looping_stream_stops_when_the_clips_end():
    link = RecordingLink()
    stream = camera_stream.CameraStream(
        three_sources(frames_per_source=2), link, fps=0, loop=False)

    assert stream.run(max_frames=10) == 2
    assert len(link.sent) == 6


def test_the_stream_paces_frames_at_the_requested_rate():
    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    link = RecordingLink()
    stream = camera_stream.CameraStream(
        three_sources(frames_per_source=3), link, fps=10,
        clock=clock, sleep=sleep)

    stream.run(max_frames=3)

    assert sum(sleeps) == pytest.approx(0.3)


def test_the_three_320_clips_are_found_by_name(tmp_path):
    for name in camera_stream.VIDEO_NAMES:
        (tmp_path / name).write_bytes(b"")

    assert camera_stream.video_paths(tmp_path) == tuple(
        tmp_path / name for name in camera_stream.VIDEO_NAMES)


def test_a_missing_clip_is_refused_by_name(tmp_path):
    (tmp_path / camera_stream.VIDEO_NAMES[0]).write_bytes(b"")
    (tmp_path / camera_stream.VIDEO_NAMES[2]).write_bytes(b"")

    with pytest.raises(FileNotFoundError, match="cam11_320.mp4"):
        camera_stream.video_paths(tmp_path)


def test_three_explicit_video_files_are_accepted(tmp_path):
    paths = [tmp_path / f"c{index}.mp4" for index in range(3)]
    for path in paths:
        path.write_bytes(b"")

    assert camera_stream.video_paths_from_files(paths) == tuple(paths)


def test_an_explicit_missing_video_is_refused_by_name(tmp_path):
    paths = [tmp_path / f"c{index}.mp4" for index in range(3)]
    paths[0].write_bytes(b"")
    paths[2].write_bytes(b"")

    with pytest.raises(FileNotFoundError, match="c1.mp4"):
        camera_stream.video_paths_from_files(paths)


def test_explicit_video_files_need_three_paths(tmp_path):
    with pytest.raises(ValueError, match="three"):
        camera_stream.video_paths_from_files([tmp_path / "c0.mp4"])


def test_the_cli_accepts_three_explicit_video_files():
    args = camera_stream.build_parser().parse_args(
        ["--video-files", "c1.mp4", "c2.mp4", "c3.mp4"])

    assert args.video_files == ["c1.mp4", "c2.mp4", "c3.mp4"]


def test_the_image_message_is_one_axial_slice():
    pytest.importorskip("pyigtl")

    message = camera_stream.image_message(
        "c1", np.zeros((4, 5), np.uint8), timestamp=7)

    assert message.image.shape == (1, 4, 5)
    assert message.device_name == "c1"
    assert message.timestamp == 7.0
    assert message.world_coordinate_system == "ras"


def test_the_image_message_carries_slicer_display_orientation():
    """The PNG path uses -R and -A; the stream must not arrive upside down."""
    pytest.importorskip("pyigtl")

    message = camera_stream.image_message(
        "c1", np.zeros((4, 5), np.uint8), timestamp=0)

    assert np.asarray(message.ijk_to_world_matrix) == pytest.approx(
        np.diag([-1.0, -1.0, 1.0, 1.0]))


def test_an_already_three_dimensional_frame_is_not_wrapped_twice():
    pytest.importorskip("pyigtl")

    message = camera_stream.image_message(
        "c3", np.zeros((1, 4, 5), np.uint8), timestamp=0)

    assert message.image.shape == (1, 4, 5)


def publish_clicks(folder, pixels, frame_id=None):
    path = pathlib.Path(folder) / "camera-clicks.json"
    payload = {"pixels": pixels, "when": "now"}
    if frame_id is not None:
        payload["frame_id"] = frame_id
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_click_without_calibration_is_refused_without_freezing(tmp_path):
    link = RecordingLink()
    stream = camera_stream.ClickableVideoStream(
        three_sources(frames_per_source=2), link, fps=0, loop=False,
        logs_dir=tmp_path, calibration=None, frame=None)

    stream.tick()
    publish_clicks(tmp_path, [[10.0, 10.0], [20.0, 20.0], [30.0, 30.0]],
                   frame_id=0)

    outcome = stream.poll_clicks()

    assert outcome is not None and outcome.status == "uncalibrated"
    assert stream.paused is False
    state = json.loads((tmp_path / "camera-stream" / "stream-state.json").read_text())
    assert state["paused"] is False
    assert state["calibrated"] is False
    result = json.loads((tmp_path / "camera-stream" / "result.json").read_text())
    assert result["status"] == "uncalibrated"
    assert list(tmp_path.glob("camera-targets-*.csv")) == []

    publish_clicks(tmp_path, [None, None, None])
    assert stream.poll_clicks() is None
    assert stream.paused is False


def test_a_pause_command_freezes_and_resume_releases(tmp_path):
    link = RecordingLink()
    stream = camera_stream.ClickableVideoStream(
        three_sources(frames_per_source=3), link, fps=0, loop=False,
        logs_dir=tmp_path, calibration=None, frame=None)
    stream.tick()
    command_path = tmp_path / "camera-stream" / "stream-command.json"

    command_path.write_text(json.dumps({"command": "pause"}), encoding="utf-8")
    stream.poll_command()
    assert stream.paused is True
    state = json.loads((tmp_path / "camera-stream" / "stream-state.json").read_text())
    assert state["paused"] is True

    time.sleep(0.01)
    command_path.write_text(json.dumps({"command": "step"}), encoding="utf-8")
    stream.poll_command()
    assert stream.paused is True
    assert len(link.sent) == 6, "one stepped frame set"
    state = json.loads((tmp_path / "camera-stream" / "stream-state.json").read_text())
    assert state["paused"] is True
    assert state["frame_id"] == 1

    time.sleep(0.01)
    command_path.write_text(json.dumps({"command": "resume"}), encoding="utf-8")
    stream.poll_command()
    assert stream.paused is False


def test_load_truth_reads_a_point_and_refuses_malformed_files(tmp_path):
    path = tmp_path / "truth.json"
    path.write_text(json.dumps({"point_mm": [1.0, 2.0, 3.0]}), encoding="utf-8")
    assert camera_stream.load_truth(path) == (1.0, 2.0, 3.0)

    path.write_text(json.dumps({"point_mm": [1.0, 2.0]}), encoding="utf-8")
    with pytest.raises(ValueError, match="three"):
        camera_stream.load_truth(path)

    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        camera_stream.load_truth(path)


def test_a_calibrated_stream_undistorts_before_sending(tmp_path):
    K = ((50.0, 0.0, 32.0), (0.0, 50.0, 32.0), (0.0, 0.0, 1.0))
    distortion = (0.5, 0.0, 0.0, 0.0, 0.0)
    projections = tuple(
        tuple(tuple(0.0 for _ in range(4)) for _ in range(3))
        for _ in range(3))
    calibration = click_solver.Calibration(
        projections=projections, width=64, height=64,
        intrinsics=(K, K, K), distortions=(distortion, distortion, distortion))
    frames = []
    for index in range(3):
        frame = np.zeros((64, 64), np.uint8)
        frame[10, 10] = 255 - index
        frame[50, 50] = 200 - index
        frames.append(frame)
    sources = [FakeSource([frame]) for frame in frames]
    link = RecordingLink()
    stream = camera_stream.ClickableVideoStream(
        sources, link, fps=0, loop=False, logs_dir=tmp_path,
        calibration=calibration, frame=Frame.load(
            pathlib.Path(__file__).resolve().parents[1] / "frame.json"))

    stream.tick()

    sent = [frame for _, frame, _ in link.sent]
    expected = [cv2.undistort(frame, np.asarray(K), np.asarray(distortion))
                for frame in frames]
    assert all(np.array_equal(actual, wanted)
               for actual, wanted in zip(sent, expected))
    assert any(not np.array_equal(actual, source)
               for actual, source in zip(sent, frames))


def test_a_calibrated_click_set_solves_on_the_published_frame(tmp_path):
    K, poses, projections = cam.scene(OPTICS, 150.0, 500.0)
    pixels = [tuple(float(value) for value in cam.project(K, R, t, np.zeros((1, 3)))[0])
              for _, R, t in poses]
    calibration = click_solver.Calibration(
        projections=tuple(tuple(tuple(float(value) for value in row) for row in matrix)
                          for matrix in projections),
        width=320, height=320)
    frame = Frame.load(pathlib.Path(__file__).resolve().parents[1] / "frame.json")
    link = RecordingLink()
    stream = camera_stream.ClickableVideoStream(
        three_sources(frames_per_source=2), link, fps=0, loop=False,
        logs_dir=tmp_path, calibration=calibration, rig=Rig(), frame=frame)

    stream.tick()
    publish_clicks(tmp_path, [list(pixel) for pixel in pixels], frame_id=0)

    outcome = stream.poll_clicks()

    assert outcome is not None and outcome.status == "found"
    assert stream.paused is False
    assert outcome.rig_point == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert max(outcome.residual_px) == pytest.approx(0.0, abs=1e-6)
    result = json.loads((tmp_path / "camera-stream" / "result.json").read_text())
    assert result["frame"] == 0
    assert result["source"] == "video"
    assert list(tmp_path.glob("camera-targets-*.csv"))


def test_an_inconsistent_click_set_writes_a_refusal_and_no_log(tmp_path):
    K, poses, projections = cam.scene(OPTICS, 150.0, 500.0)
    pixels = [tuple(float(value) for value in cam.project(K, R, t, np.zeros((1, 3)))[0])
              for _, R, t in poses]
    pixels[1] = (pixels[1][0] + 20.0, pixels[1][1] - 20.0)
    calibration = click_solver.Calibration(
        projections=tuple(tuple(tuple(float(value) for value in row) for row in matrix)
                          for matrix in projections),
        width=320, height=320)
    frame = Frame.load(pathlib.Path(__file__).resolve().parents[1] / "frame.json")
    link = RecordingLink()
    stream = camera_stream.ClickableVideoStream(
        three_sources(frames_per_source=2), link, fps=0, loop=False,
        logs_dir=tmp_path, calibration=calibration, rig=Rig(), frame=frame)

    stream.tick()
    publish_clicks(tmp_path, [list(pixel) for pixel in pixels], frame_id=0)

    outcome = stream.poll_clicks()

    assert outcome is not None and outcome.status == "inconsistent"
    assert outcome.ras == ()
    result = json.loads((tmp_path / "camera-stream" / "result.json").read_text())
    assert result["status"] == "inconsistent"
    assert "residual" in result["message"]
    assert not list(tmp_path.glob("camera-targets-*.csv"))
