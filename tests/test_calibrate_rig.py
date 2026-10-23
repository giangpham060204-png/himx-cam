"""Offline calibration: recover a known three-camera rig from rendered boards.

These are real images, not point lists: the checkerboard is drawn by projecting
a board pattern through a known camera, then read back with OpenCV's detector.
The expected K and relative poses are therefore independent of the code under
test, and a wrong unit (metres vs millimetres), a mirrored board, or a bad
extrinsic shows up as a number that disagrees with the truth.
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

import calibrate_rig  # noqa: E402
import click_solver  # noqa: E402
import stereo_board  # noqa: E402
import synthetic_camera as cam  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402
from targeting.triangulation import camera_pose  # noqa: E402

BOARD = (9, 6)
SQUARE_MM = 25.0
IMAGE_SIZE = (640, 480)
TRUE_K = np.array([[600.0, 0.0, 320.0],
                   [0.0, 600.0, 240.0],
                   [0.0, 0.0, 1.0]])
BOARD_CENTRE = np.array([BOARD[0] * SQUARE_MM / 2.0,
                         BOARD[1] * SQUARE_MM / 2.0, 0.0])
CAMERA_POSITIONS = (np.array([0.0, 0.0, 0.0]),
                    np.array([150.0, 0.0, 0.0]),
                    np.array([0.0, 150.0, 0.0]))
CAMERA_TARGET = (0.0, 0.0, 400.0)
BOARD_DISTANCE_MM = 400.0


def true_camera_poses():
    """``(R, t)`` mapping world millimetres to each camera."""
    poses = []
    for position in CAMERA_POSITIONS:
        R, t = camera_pose(tuple(position), CAMERA_TARGET)
        poses.append((np.asarray(R, float), np.asarray(t, float)))
    return poses


def render_board(K, R, t, scale=4.0, margin=40):
    """Draw a 9x6 inner-corner board under ``K [R|t]``.

    The pattern is one source image, warped by the exact homography that takes
    board millimetres to pixels, so the drawn image and the expected geometry
    cannot drift apart.
    """
    cols, rows = BOARD
    squares_x, squares_y = cols + 1, rows + 1
    pattern = np.full((int(squares_y * SQUARE_MM * scale) + 2 * margin,
                       int(squares_x * SQUARE_MM * scale) + 2 * margin),
                      255, np.uint8)
    for row in range(squares_y):
        for col in range(squares_x):
            if (row + col) % 2:
                continue
            y0 = margin + int(row * SQUARE_MM * scale)
            x0 = margin + int(col * SQUARE_MM * scale)
            side = int(SQUARE_MM * scale)
            pattern[y0:y0 + side, x0:x0 + side] = 0
    px_to_mm = np.array([
        [1.0 / scale, 0.0, -(margin / scale + SQUARE_MM)],
        [0.0, 1.0 / scale, -(margin / scale + SQUARE_MM)],
        [0.0, 0.0, 1.0],
    ])
    homography = K @ np.column_stack([R[:, 0], R[:, 1], t]) @ px_to_mm
    return cv2.warpPerspective(pattern, homography, IMAGE_SIZE,
                               flags=cv2.INTER_LINEAR, borderValue=255)


def synthetic_captures(count=20, seed=3):
    """Board images for all three cameras, and the true rig they were made with."""
    rng = np.random.default_rng(seed)
    cameras = true_camera_poses()
    images = [[], [], []]
    board_poses = []
    for _ in range(count):
        tilt = rng.uniform(-0.30, 0.30, 3)
        offset = rng.uniform(-60.0, 60.0, 2)
        R_board = cv2.Rodrigues(tilt)[0]
        centre = np.array([offset[0], offset[1], BOARD_DISTANCE_MM])
        t_board = centre - R_board @ BOARD_CENTRE
        board_poses.append((R_board, t_board))
        for index, (R_camera, t_camera) in enumerate(cameras):
            R_board_to_camera = R_camera @ R_board
            t_board_to_camera = R_camera @ t_board + t_camera
            images[index].append(render_board(
                TRUE_K, R_board_to_camera, t_board_to_camera))
    return images, cameras


def barrel_distorted(images, k1=-0.08):
    """Bend pinhole board images with a known Brown k1.

    ``distortion_maps`` says where each pixel of the bent frame samples the
    ideal one, which is exactly the remap a real lens would impose.
    """
    mapx, mapy = cam.distortion_maps(TRUE_K, k1, IMAGE_SIZE)
    return [[cv2.remap(image, mapx, mapy, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
             for image in camera_images]
            for camera_images in images]


def relative_true(cameras, index):
    """True transform from camera 1 into camera ``index``, in millimetres."""
    R1, t1 = cameras[0]
    Ri, ti = cameras[index]
    R = Ri @ R1.T
    t = ti - R @ t1
    return R, t


def rotation_angle_degrees(R):
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))))


def test_calibration_recovers_known_intrinsics_and_baselines():
    images, cameras = synthetic_captures()

    result = calibrate_rig.calibrate_rig(
        images, board=BOARD, square_mm=SQUARE_MM, image_size=IMAGE_SIZE,
        zero_distortion=True)

    assert [camera["name"] for camera in result["cameras"]] == ["c1", "c2", "c3"]
    for index, camera in enumerate(result["cameras"]):
        K = np.asarray(camera["K"], float)
        assert camera["views"] >= 15
        assert camera["rms"] < 1.0
        assert K[0, 0] == pytest.approx(TRUE_K[0, 0], abs=5.0)
        assert K[1, 1] == pytest.approx(TRUE_K[1, 1], abs=5.0)
        assert K[0, 2] == pytest.approx(TRUE_K[0, 2], abs=5.0)
        assert K[1, 2] == pytest.approx(TRUE_K[1, 2], abs=5.0)
        assert np.allclose(camera["distortion"], 0.0)
        assert np.asarray(camera["projection"], float).shape == (3, 4)

    for index in (1, 2):
        camera = result["cameras"][index]
        K = np.asarray(camera["K"], float)
        projection = np.asarray(camera["projection"], float)
        pose = np.linalg.inv(K) @ projection
        R_measured = pose[:, :3]
        t_measured = pose[:, 3]
        R_true, t_true = relative_true(cameras, index)
        assert rotation_angle_degrees(R_measured @ R_true.T) < 0.5
        assert np.linalg.norm(t_measured - t_true) < 2.0
        assert np.linalg.norm(t_measured) == pytest.approx(
            np.linalg.norm(t_true), abs=2.0)


def test_calibration_recovers_a_barrel_distorted_rig(tmp_path):
    images, cameras = synthetic_captures()
    images = barrel_distorted(images, k1=-0.08)

    result = calibrate_rig.calibrate_rig(
        images, board=BOARD, square_mm=SQUARE_MM, image_size=IMAGE_SIZE,
        zero_distortion=False)

    for camera in result["cameras"]:
        K = np.asarray(camera["K"], float)
        assert K[0, 0] == pytest.approx(TRUE_K[0, 0], abs=5.0)
        assert K[1, 1] == pytest.approx(TRUE_K[1, 1], abs=5.0)
        assert K[0, 2] == pytest.approx(TRUE_K[0, 2], abs=5.0)
        assert K[1, 2] == pytest.approx(TRUE_K[1, 2], abs=5.0)
        assert camera["rms"] < 1.0
        assert camera["distortion"][0] == pytest.approx(-0.08, abs=0.05)

    for index in (1, 2):
        camera = result["cameras"][index]
        K = np.asarray(camera["K"], float)
        projection = np.asarray(camera["projection"], float)
        pose = np.linalg.inv(K) @ projection
        R_measured = pose[:, :3]
        t_measured = pose[:, 3]
        R_true, t_true = relative_true(cameras, index)
        assert rotation_angle_degrees(R_measured @ R_true.T) < 0.5
        assert np.linalg.norm(t_measured - t_true) < 2.0

    path = tmp_path / "camera-rig.json"
    calibrate_rig.write_rig_file(result, path)
    loaded = click_solver.load_calibration(path)
    assert loaded.needs_undistort is True


def test_calibration_writes_a_file_the_stream_can_load(tmp_path):
    images, _ = synthetic_captures(count=12)
    result = calibrate_rig.calibrate_rig(
        images, board=BOARD, square_mm=SQUARE_MM, image_size=IMAGE_SIZE,
        zero_distortion=True)
    path = tmp_path / "camera-rig.json"

    calibrate_rig.write_rig_file(result, path)

    calibration = click_solver.load_calibration(path)
    assert len(calibration.projections) == 3
    assert (calibration.width, calibration.height) == IMAGE_SIZE
    assert all(np.allclose(distortion, 0.0) for distortion in calibration.distortions)


def test_calibration_skips_one_unreadable_board_but_keeps_the_rig():
    images, _ = synthetic_captures(count=12)
    images[1][3] = np.zeros_like(images[1][3])

    result = calibrate_rig.calibrate_rig(
        images, board=BOARD, square_mm=SQUARE_MM, image_size=IMAGE_SIZE,
        zero_distortion=True)

    assert [camera["views"] for camera in result["cameras"]] == [12, 11, 12]
    assert result["cameras"][1]["rms"] < 1.0


def test_calibration_can_read_a_capture_folder(tmp_path):
    images, _ = synthetic_captures(count=12)
    for camera_index, camera_images in enumerate(images):
        folder = tmp_path / f"c{camera_index + 1}"
        folder.mkdir()
        for pose_index, image in enumerate(camera_images):
            cv2.imwrite(str(folder / f"pose{pose_index:02d}.png"), image)

    result = calibrate_rig.calibrate_from_folders(
        tmp_path, board=BOARD, square_mm=SQUARE_MM, zero_distortion=True)

    assert len(result["cameras"]) == 3
    assert result["image_size"] == list(IMAGE_SIZE)


def test_calibration_cli_writes_a_loadable_file(tmp_path):
    images, _ = synthetic_captures(count=12)
    for camera_index, camera_images in enumerate(images):
        folder = tmp_path / f"c{camera_index + 1}"
        folder.mkdir()
        for pose_index, image in enumerate(camera_images):
            cv2.imwrite(str(folder / f"pose{pose_index:02d}.png"), image)
    out = tmp_path / "camera-rig.json"

    status = calibrate_rig.main([
        "--captures", str(tmp_path), "--out", str(out),
        "--zero-distortion",
    ])

    assert status == 0
    calibration = click_solver.load_calibration(out)
    assert len(calibration.projections) == 3
    assert (calibration.width, calibration.height) == IMAGE_SIZE


def test_calibration_refuses_a_missing_camera_folder(tmp_path):
    images, _ = synthetic_captures(count=3)
    for camera_index, camera_images in enumerate(images[:2]):
        folder = tmp_path / f"c{camera_index + 1}"
        folder.mkdir()
        for pose_index, image in enumerate(camera_images):
            cv2.imwrite(str(folder / f"pose{pose_index:02d}.png"), image)

    with pytest.raises(ValueError, match="c3"):
        calibrate_rig.calibrate_from_folders(
            tmp_path, board=BOARD, square_mm=SQUARE_MM)


def test_calibration_refuses_too_few_views(tmp_path):
    images, _ = synthetic_captures(count=2)
    for camera_index, camera_images in enumerate(images):
        folder = tmp_path / f"c{camera_index + 1}"
        folder.mkdir()
        for pose_index, image in enumerate(camera_images):
            cv2.imwrite(str(folder / f"pose{pose_index:02d}.png"), image)

    with pytest.raises(ValueError, match="at least 3"):
        calibrate_rig.calibrate_from_folders(
            tmp_path, board=BOARD, square_mm=SQUARE_MM)
