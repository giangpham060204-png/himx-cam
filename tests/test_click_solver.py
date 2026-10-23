"""Tests for the calibrated click solver and its calibration contract."""

import json
import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")
pytest.importorskip("matplotlib")

import click_solver  # noqa: E402
import synthetic_camera as cam  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402

OPTICS = optics_for()
FRAME = Frame.load(pathlib.Path(__file__).resolve().parents[1] / "frame.json")


def scene():
    K, poses, projections = cam.scene(OPTICS, 150.0, 500.0)
    pixels = [tuple(float(value) for value in cam.project(K, R, t, np.zeros((1, 3)))[0])
              for _, R, t in poses]
    return K, poses, projections, pixels


def test_a_known_point_solves_and_has_no_residual():
    _, _, projections, pixels = scene()

    measurement = click_solver.solve_click_set(
        projections, pixels, Rig(), FRAME, truth=(0.0, 0.0, 0.0))

    assert measurement.status == "found"
    assert measurement.rig_point == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert measurement.ras == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert measurement.cube == pytest.approx((50.0, 50.0, 50.0), abs=1e-6)
    assert measurement.error_mm == pytest.approx(0.0, abs=1e-6)
    assert max(measurement.residual_px) == pytest.approx(0.0, abs=1e-6)


def test_a_misclicked_pixel_shows_up_as_a_residual():
    _, _, projections, pixels = scene()
    pixels = list(pixels)
    pixels[1] = (pixels[1][0] + 6.0, pixels[1][1] - 4.0)

    measurement = click_solver.solve_click_set(projections, pixels, Rig(), FRAME)

    assert measurement.status == "found"
    assert max(measurement.residual_px) > 2.0


def test_a_click_set_with_large_residuals_is_refused_not_found():
    _, _, projections, pixels = scene()
    pixels = list(pixels)
    pixels[1] = (pixels[1][0] + 20.0, pixels[1][1] - 20.0)

    measurement = click_solver.solve_click_set(
        projections, pixels, Rig(), FRAME, truth=(0.0, 0.0, 0.0))

    assert measurement.status == "inconsistent"
    assert max(measurement.residual_px) > 5.0
    assert measurement.ras == ()
    assert measurement.cube == ()
    assert "no point" in measurement.message
    assert "residual" in measurement.message


def test_a_point_outside_the_field_is_refused_with_the_axis():
    _, _, projections, pixels = scene()

    measurement = click_solver.solve_click_set(
        projections, pixels, Rig(translation_mm=(500.0, 0.0, 0.0)), FRAME)

    assert measurement.status == "outside-field"
    assert "R=" in measurement.message


def write_calibration(path, projections, width=320, height=320):
    payload = {"cameras": [
        {"name": f"c{index + 1}", "projection": np.asarray(projection).tolist(),
         "width": width, "height": height}
        for index, projection in enumerate(projections)
    ]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_calibration_file_round_trips_three_projections(tmp_path):
    _, _, projections, _ = scene()
    path = write_calibration(tmp_path / "camera-rig.json", projections)

    calibration = click_solver.load_calibration(path)

    assert len(calibration.projections) == 3
    assert (calibration.width, calibration.height) == (320, 320)
    assert np.asarray(calibration.projections[0]).shape == (3, 4)
    assert calibration.demo is False
    assert calibration.max_residual_px == pytest.approx(5.0)


def test_a_demo_calibration_carries_its_own_residual_limit(tmp_path):
    _, _, projections, _ = scene()
    payload = {"demo": True, "max_residual_px": 17.5, "cameras": [
        {"name": f"c{index + 1}", "projection": np.asarray(projection).tolist(),
         "width": 320, "height": 320}
        for index, projection in enumerate(projections)
    ]}
    path = tmp_path / "camera-rig.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    calibration = click_solver.load_calibration(path)

    assert calibration.demo is True
    assert calibration.max_residual_px == pytest.approx(17.5)


def test_a_demo_limit_can_accept_a_residual_the_default_refuses():
    _, _, projections, pixels = scene()
    pixels = list(pixels)
    pixels[1] = (pixels[1][0] + 20.0, pixels[1][1] - 20.0)

    default = click_solver.solve_click_set(projections, pixels, Rig(), FRAME)
    diagnostic = click_solver.solve_click_set(
        projections, pixels, Rig(), FRAME, max_residual_px=1e9)
    accepted = click_solver.solve_click_set(
        projections, pixels, Rig(), FRAME,
        max_residual_px=max(diagnostic.residual_px) + 1.0)

    assert default.status == "inconsistent"
    assert accepted.status == "found"


def test_a_calibration_with_fewer_than_three_cameras_is_refused(tmp_path):
    _, _, projections, _ = scene()
    payload = {"cameras": [{"name": "c1",
                            "projection": np.asarray(projections[0]).tolist(),
                            "width": 320, "height": 320}]}
    path = tmp_path / "camera-rig.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="three cameras"):
        click_solver.load_calibration(path)


def test_a_calibration_with_a_bad_projection_shape_is_refused(tmp_path):
    _, _, projections, _ = scene()
    path = write_calibration(tmp_path / "camera-rig.json", projections)
    payload = json.loads(path.read_text())
    payload["cameras"][1]["projection"] = [[1.0, 2.0]]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="3x4"):
        click_solver.load_calibration(path)


def test_a_calibration_with_K_and_distortion_is_loaded_and_applied(tmp_path):
    _, _, projections, _ = scene()
    K = [[50.0, 0.0, 32.0], [0.0, 50.0, 32.0], [0.0, 0.0, 1.0]]
    distortion = [0.5, 0.0, 0.0, 0.0, 0.0]
    payload = {"cameras": [
        {"name": f"c{index + 1}", "projection": np.asarray(projection).tolist(),
         "K": K, "distortion": distortion, "width": 64, "height": 64}
        for index, projection in enumerate(projections)
    ]}
    path = tmp_path / "camera-rig.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    calibration = click_solver.load_calibration(path)
    frame = np.zeros((64, 64), np.uint8)
    frame[10, 10] = 255
    frame[50, 50] = 200
    corrected = calibration.undistort(0, frame)

    assert calibration.needs_undistort is True
    assert corrected.shape == frame.shape
    assert not np.array_equal(corrected, frame)


def test_distortion_without_K_is_refused(tmp_path):
    _, _, projections, _ = scene()
    path = write_calibration(tmp_path / "camera-rig.json", projections)
    payload = json.loads(path.read_text())
    payload["cameras"][0]["distortion"] = [0.1, 0.0, 0.0, 0.0, 0.0]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="distortion without K"):
        click_solver.load_calibration(path)
