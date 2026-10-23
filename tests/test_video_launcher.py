"""The Slicer-side launcher for the external camera-stream sender.

The dialog only collects paths; these tests pin the command it will run and the
process lifecycle it owns. No Slicer import is allowed here: the launcher is
plain Python so it can be tested on the host.
"""

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.video_launcher import (  # noqa: E402
    LauncherError,
    VideoProcess,
    build_command,
    validate_video_files,
)


def three_files(tmp_path):
    paths = [tmp_path / f"cam{index}.mp4" for index in range(3)]
    for path in paths:
        path.write_bytes(b"")
    return paths


def test_the_command_names_three_files_and_the_optional_inputs(tmp_path):
    files = three_files(tmp_path)
    calibration = tmp_path / "camera-rig.json"
    truth = tmp_path / "truth.json"
    calibration.write_text("{}")
    truth.write_text("{}")

    command = build_command(
        files, calibration=calibration, truth=truth, fps=8.5, port=18945,
        uv="/usr/bin/uv")

    assert command[:4] == [
        "/usr/bin/uv", "run", "tools/camera_stream.py", "--video-files"]
    assert command[4:7] == [str(path) for path in files]
    assert command[command.index("--fps") + 1] == "8.5"
    assert command[command.index("--port") + 1] == "18945"
    assert command[command.index("--calibration") + 1] == str(calibration)
    assert command[command.index("--truth") + 1] == str(truth)


def test_three_files_are_required(tmp_path):
    with pytest.raises(LauncherError, match="three"):
        validate_video_files(three_files(tmp_path)[:2])


def test_a_missing_video_is_refused_by_name(tmp_path):
    files = three_files(tmp_path)
    files[1].unlink()

    with pytest.raises(LauncherError, match=files[1].name):
        validate_video_files(files)


def test_a_missing_calibration_is_refused(tmp_path):
    files = three_files(tmp_path)

    with pytest.raises(LauncherError, match="camera-rig.json"):
        build_command(files, calibration=tmp_path / "camera-rig.json")


def test_the_frame_rate_must_be_positive(tmp_path):
    files = three_files(tmp_path)

    with pytest.raises(LauncherError, match="fps"):
        build_command(files, fps=0)


def test_a_started_sender_is_running_until_it_is_stopped(tmp_path):
    process = VideoProcess(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path, log_path=tmp_path / "sender.log")

    started = process.start()
    try:
        assert started["status"] == "running"
        assert process.is_running()
        assert started["pid"] > 0
    finally:
        stopped = process.stop()

    assert stopped["status"] == "stopped"
    assert not process.is_running()


def test_an_exited_sender_reports_its_code_and_log_tail(tmp_path):
    process = VideoProcess(
        [sys.executable, "-c",
         "print('sender says hi'); raise SystemExit(7)"],
        cwd=tmp_path, log_path=tmp_path / "sender.log")
    process.start()

    deadline = time.time() + 5
    status = process.poll()
    while status["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
        status = process.poll()

    assert status["status"] == "exited"
    assert status["returncode"] == 7
    assert "sender says hi" in status["log_tail"]
