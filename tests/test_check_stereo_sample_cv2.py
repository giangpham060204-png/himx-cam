"""Tests for the parts of the stereo-sample check that need OpenCV.

These pin the layers that only exist because OpenCV exists: reading the
published answer key back out of its YAML, finding the board, calibrating a
camera, and turning pixels back into a 3D point. Everything here is driven
by images rendered from a known camera, so the expected answer is known
independently of the code under test.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

cv2 = pytest.importorskip("cv2")

import check_stereo_sample as chk  # noqa: E402
import stereo_board  # noqa: E402

COLS, ROWS, SQUARE = 9, 6, 0.025
BOARD_CENTRE = np.array([COLS * SQUARE / 2, ROWS * SQUARE / 2, 0.0])
TRUE_K = np.array([[800.0, 0.0, 320.0], [0.0, 800.0, 240.0], [0.0, 0.0, 1.0]])
IMAGE_SIZE = (640, 480)

REFERENCE_YAML = """\
%YAML:1.0
---
nframes: 13
image_width: 640
image_height: 480
board_width: 9
board_height: 6
square_size: 2.5000000372529030e-02
avg_reprojection_error: 3.9259098975581364e-01
camera_matrix: !!opencv-matrix
   rows: 3
   cols: 3
   dt: d
   data: [ 5.3591573396163199e+02, 0., 3.4228315473308373e+02, 0.,
       5.3591573396163199e+02, 2.3557082909788173e+02, 0., 0., 1. ]
distortion_coefficients: !!opencv-matrix
   rows: 5
   cols: 1
   dt: d
   data: [ -2.6637260909660682e-01, -3.8588898922304653e-02,
       1.7831947042852964e-03, -2.8122100441115472e-04,
       2.3839153080878486e-01 ]
"""

SAMPLE_DIR = pathlib.Path(__file__).resolve().parents[1] / "data" / "opencv-stereo-sample"
needs_sample = pytest.mark.skipif(
    not (SAMPLE_DIR / "left_intrinsics.yml").exists(),
    reason="run tools/check_stereo_sample.py to fetch OpenCV's sample",
)


# --- rendering a board the code has never seen ----------------------------


def render_board(K, R, t, scale=4000.0, margin=40, image_size=IMAGE_SIZE, border=255):
    """Draw a chessboard as seen by a camera with calibration ``K``.

    The pattern is built flat and then warped by the exact homography that
    ``K``, ``R`` and ``t`` imply, so the drawn image and the board geometry
    cannot drift apart -- which is what makes the expected corner pixels
    trustworthy.
    """
    squares_x, squares_y = COLS + 1, ROWS + 1
    pattern = np.full((int(squares_y * SQUARE * scale) + 2 * margin,
                       int(squares_x * SQUARE * scale) + 2 * margin), 255, np.uint8)
    for row in range(squares_y):
        for col in range(squares_x):
            if (row + col) % 2:
                continue
            y0 = margin + int(row * SQUARE * scale)
            x0 = margin + int(col * SQUARE * scale)
            pattern[y0:y0 + int(SQUARE * scale), x0:x0 + int(SQUARE * scale)] = 0

    px_to_metres = np.array([[1.0 / scale, 0.0, -(margin / scale + SQUARE)],
                             [0.0, 1.0 / scale, -(margin / scale + SQUARE)],
                             [0.0, 0.0, 1.0]])
    homography = K @ np.column_stack([R[:, 0], R[:, 1], t]) @ px_to_metres
    return cv2.warpPerspective(pattern, homography, image_size,
                               flags=cv2.INTER_LINEAR, borderValue=border)


def pose(rvec, distance, offset=(0.0, 0.0)):
    """A board pose placing the board's centre at ``offset`` in the field of view."""
    R = cv2.Rodrigues(np.asarray(rvec, dtype=float))[0]
    t = -R @ BOARD_CENTRE + np.array([offset[0], offset[1], distance])
    return R, t


def synthetic_views(count=12):
    """Board views varied enough to constrain a calibration.

    Poses swing both in tilt and across the field of view: a board that is
    always centred is the textbook case where the principal point cannot be
    separated from the focal length, and a calibration that fixes fx = fy
    while never moving the board will happily report a centre twenty pixels
    from where it belongs.
    """
    tilts = [(0.0, 0.2, 0.0), (0.25, 0.0, 0.1), (-0.2, -0.15, 0.05), (0.15, 0.3, -0.1),
             (0.3, -0.2, 0.2), (-0.3, 0.1, -0.15), (0.1, -0.3, 0.1), (0.2, 0.25, 0.3),
             (-0.15, 0.2, -0.25), (0.35, 0.05, 0.0), (-0.25, -0.3, 0.15), (0.05, 0.35, -0.2)]
    offsets = [(0.0, 0.0), (0.06, 0.04), (-0.06, -0.04), (0.0, 0.05),
               (0.07, 0.0), (-0.07, 0.03), (0.03, -0.05), (-0.04, 0.06),
               (0.05, -0.03), (-0.05, -0.05), (0.02, 0.04), (-0.03, 0.0)]
    return [(pose(tilt, 0.65 + 0.05 * i, offsets[i]), tilt) for i, tilt in enumerate(tilts[:count])]


# --- reading the published answer key -------------------------------------


def test_load_reference_intrinsics_reads_opencvs_own_yaml(tmp_path):
    path = tmp_path / "left_intrinsics.yml"
    path.write_text(REFERENCE_YAML)

    reference = chk.load_reference_intrinsics(path)

    assert reference["frames"] == 13
    assert reference["board"] == (9, 6)
    assert reference["image_size"] == (640, 480)
    assert reference["square_size"] == pytest.approx(0.025)
    assert reference["rms"] == pytest.approx(0.39259098975581364)
    assert reference["fx"] == pytest.approx(535.91573396163199)
    assert reference["fy"] == pytest.approx(535.91573396163199)
    assert reference["cx"] == pytest.approx(342.28315473308373)
    assert reference["cy"] == pytest.approx(235.57082909788173)
    assert reference["distortion"].shape == (5,)
    assert reference["distortion"][0] == pytest.approx(-0.26637260909660682)


def test_load_reference_intrinsics_refuses_a_file_it_cannot_read(tmp_path):
    with pytest.raises(RuntimeError, match="left_intrinsics.yml"):
        chk.load_reference_intrinsics(tmp_path / "left_intrinsics.yml")


# --- finding the board ----------------------------------------------------


def test_detect_board_finds_corners_in_the_order_the_board_declares():
    R, t = pose((0.1, 0.1, 0.0), 0.7)
    image = render_board(TRUE_K, R, t)
    expected, _ = cv2.projectPoints(stereo_board.object_points(COLS, ROWS, SQUARE),
                                    R, t, TRUE_K, None)

    corners = chk.detect_board(image, (COLS, ROWS))

    assert corners.shape == (COLS * ROWS, 2)
    assert np.allclose(corners, expected.reshape(-1, 2), atol=1.0)


def test_detect_board_says_so_when_there_is_no_board():
    blank = np.full((480, 640), 128, np.uint8)

    with pytest.raises(RuntimeError, match="9x6"):
        chk.detect_board(blank, (COLS, ROWS))


# --- calibrating ----------------------------------------------------------


def test_projection_matrix_agrees_with_opencv_projectpoints():
    R, t = pose((0.2, -0.1, 0.15), 0.8)
    points = stereo_board.object_points(COLS, ROWS, SQUARE)

    P = stereo_board.projection_matrix(TRUE_K, R, t)
    homogeneous = np.column_stack([points, np.ones(len(points))]) @ P.T
    ours = homogeneous[:, :2] / homogeneous[:, 2:3]

    theirs, _ = cv2.projectPoints(points, R, t, TRUE_K, None)
    assert np.allclose(ours, theirs.reshape(-1, 2), atol=1e-9)


def test_calibrate_monocular_recovers_the_camera_it_was_rendered_with():
    views = synthetic_views()
    images = [render_board(TRUE_K, R, t) for (R, t), _ in views]

    result = chk.calibrate_monocular([chk.detect_board(i, (COLS, ROWS)) for i in images],
                                     (COLS, ROWS), SQUARE, IMAGE_SIZE)

    assert result["rms"] < 0.2
    assert stereo_board.compare_intrinsics(
        {"fx": TRUE_K[0, 0], "fy": TRUE_K[1, 1], "cx": TRUE_K[0, 2], "cy": TRUE_K[1, 2]},
        result,
    ) == []


# --- a pair of cameras ----------------------------------------------------

# A plausible rig: the right camera 100 mm to the side of the left one and
# turned to look at the same place, so that a board in the working volume
# stays inside both fields of view.
RIGHT_POSITION = np.array([0.1, 0.0, 0.0])
SCENE_CENTRE = np.array([0.0, 0.0, 0.65])


def look_at(position, target):
    """Rotation from the left camera's frame into a camera at ``position`` looking at ``target``."""
    forward = (target - position) / np.linalg.norm(target - position)
    right = np.cross(np.array([0.0, 1.0, 0.0]), forward)
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    return np.vstack([right, down, forward])


RIGHT_R = look_at(RIGHT_POSITION, SCENE_CENTRE)
RIGHT_T = -RIGHT_R @ RIGHT_POSITION

# In the region of the sample's own distortion, k1 = -0.27.
DISTORTION = np.array([-0.27, 0.09, 0.001, -0.0005, 0.0])


def as_second_camera(R, t):
    """The board pose ``(R, t)`` expressed in the right camera's frame."""
    return RIGHT_R @ R, RIGHT_R @ t + RIGHT_T


def test_triangulate_points_finds_the_points_the_cameras_looked_at():
    points = np.array([[0.0, 0.0, 0.65], [0.05, -0.03, 0.8], [-0.08, 0.06, 0.6]])
    P_left = stereo_board.projection_matrix(TRUE_K, np.eye(3), np.zeros(3))
    P_right = stereo_board.projection_matrix(TRUE_K, RIGHT_R, RIGHT_T)

    left, _ = cv2.projectPoints(points, np.eye(3), np.zeros(3), TRUE_K, None)
    right, _ = cv2.projectPoints(points, RIGHT_R, RIGHT_T, TRUE_K, None)
    recovered = chk.triangulate_points(P_left, P_right,
                                       left.reshape(-1, 2), right.reshape(-1, 2))

    assert np.allclose(recovered, points, atol=1e-6)


def test_rectified_triangulation_survives_a_lens_that_bends_the_image():
    """Straight pixels would bend the board.

    A lens this distorted makes a plane come back saddle-shaped if the pixels
    are fed to the rectified projection matrices as they were measured, so the
    rectification step has to be part of the way a point is reconstructed.
    """
    R, t = pose((0.15, -0.1, 0.05), 0.65)
    R2, t2 = as_second_camera(R, t)
    board_points = stereo_board.object_points(COLS, ROWS, SQUARE)
    left, _ = cv2.projectPoints(board_points, R, t, TRUE_K, DISTORTION)
    right, _ = cv2.projectPoints(board_points, R2, t2, TRUE_K, DISTORTION)
    R1, R2r, P1, P2, _, _, _ = cv2.stereoRectify(TRUE_K, DISTORTION, TRUE_K, DISTORTION,
                                                 IMAGE_SIZE, RIGHT_R, RIGHT_T.reshape(3, 1),
                                                 alpha=0.0)

    recovered = chk.triangulate_points(
        P1, P2,
        chk.rectified_points(left.reshape(-1, 2), TRUE_K, DISTORTION, R1, P1),
        chk.rectified_points(right.reshape(-1, 2), TRUE_K, DISTORTION, R2r, P2),
    )

    centroid, normal = stereo_board.fit_plane(recovered)
    assert stereo_board.planar_residuals(recovered, centroid, normal).max() < 1e-6
    assert np.allclose(stereo_board.grid_side_lengths(recovered, COLS, ROWS),
                       SQUARE, atol=1e-6)


def test_calibrate_stereo_recovers_the_pair_it_was_rendered_with():
    left_views, right_views = [], []
    for (R, t), _ in synthetic_views():
        left_views.append(chk.detect_board(render_board(TRUE_K, R, t), (COLS, ROWS)))
        R2, t2 = as_second_camera(R, t)
        right_views.append(chk.detect_board(render_board(TRUE_K, R2, t2), (COLS, ROWS)))

    mono_left = chk.calibrate_monocular(left_views, (COLS, ROWS), SQUARE, IMAGE_SIZE)
    mono_right = chk.calibrate_monocular(right_views, (COLS, ROWS), SQUARE, IMAGE_SIZE)
    stereo = chk.calibrate_stereo(left_views, right_views, mono_left, mono_right,
                                  (COLS, ROWS), SQUARE, IMAGE_SIZE)

    rotation_error = np.rad2deg(np.arccos(np.clip((np.trace(RIGHT_R.T @ stereo["R"]) - 1) / 2, -1, 1)))
    assert rotation_error < 1.0
    assert np.linalg.norm(stereo["T"].ravel() - RIGHT_T) < 5e-3
    # a correct pair rectifies to matching rows; a whole pixel is the natural
    # unit and the median here is nearer a tenth of one
    residuals = chk.rectified_vertical_residuals(left_views[-1], right_views[-1], stereo)
    assert np.median(residuals) < 0.2
    assert residuals.max() < 1.0


# --- the real thing -------------------------------------------------------


@needs_sample
def test_our_calibration_of_the_sample_matches_opencvs_published_one():
    reference = chk.load_reference_intrinsics(SAMPLE_DIR / "left_intrinsics.yml")

    result = chk.calibrate_monocular(chk.load_views(SAMPLE_DIR, "left"), reference["board"],
                                     reference["square_size"], reference["image_size"])

    # Same images, same board, so the numbers should land on top of the
    # published ones rather than merely near them: half a percent and one
    # pixel are generous next to the hundredth of a percent measured.
    assert stereo_board.compare_intrinsics(reference, result,
                                           rel_tol=0.005, abs_tol_px=1.0) == []
    assert result["rms"] < 1.5 * reference["rms"]


@needs_sample
def test_the_sample_reconstructs_to_a_rigid_planar_board():
    reference = chk.load_reference_intrinsics(SAMPLE_DIR / "left_intrinsics.yml")
    board, square = reference["board"], reference["square_size"]

    left_views = chk.load_views(SAMPLE_DIR, "left")
    right_views = chk.load_views(SAMPLE_DIR, "right")
    mono_left = chk.calibrate_monocular(left_views, board, square, reference["image_size"])
    mono_right = chk.calibrate_monocular(right_views, board, square, reference["image_size"])
    stereo = chk.calibrate_stereo(left_views, right_views, mono_left, mono_right,
                                  board, square, reference["image_size"])

    points = chk.triangulate_points(
        stereo["P1"], stereo["P2"],
        chk.rectified_points(left_views[0], stereo["K1"], stereo["d1"], stereo["R1"], stereo["P1"]),
        chk.rectified_points(right_views[0], stereo["K2"], stereo["d2"], stereo["R2"], stereo["P2"]),
    )
    centroid, normal = stereo_board.fit_plane(points)

    # The board is a rigid grid of 25 mm squares, so it comes back one square
    # wide per step and flat. These tolerances are millimetres because the
    # sample's own baseline is only 84 mm against a working distance near
    # 380 mm, where a pixel of corner error is worth some 3 mm of depth.
    sides = stereo_board.grid_side_lengths(points, *board)
    assert sides.mean() == pytest.approx(square, rel=0.01)
    assert sides.std() < 0.1 * square

    residuals = stereo_board.planar_residuals(points, centroid, normal)
    assert residuals.mean() < 0.05 * square
    assert np.median(residuals) < 0.02 * square

    # and the pair rectifies to matching rows
    rows = chk.rectified_vertical_residuals(left_views[0], right_views[0], stereo)
    assert np.median(rows) < 0.5
    assert rows.max() < 2.0


# --- the report -----------------------------------------------------------


def report_fixture(checks):
    """A report of the shape ``build_report`` returns, for exercising the printer."""
    return {
        "cache_dir": "data/opencv-stereo-sample",
        "reference": {"frames": 13, "board": (9, 6), "square_size": 0.025,
                      "image_size": (640, 480), "fx": 535.92, "fy": 535.92,
                      "cx": 342.28, "cy": 235.57, "rms": 0.3926,
                      "distortion": np.zeros(5)},
        "mono": {"fx": 536.0, "fy": 536.0, "cx": 352.28, "cy": 235.6,
                 "rms": 0.41, "distortion": np.zeros(5)},
        "measurements": {"depth_mm": 384.0, "baseline_mm": 83.6, "square_mean_mm": 25.013,
                         "square_std_mm": 0.44, "flatness_mean_mm": 0.79,
                         "flatness_median_mm": 0.465, "row_median_px": 0.147,
                         "row_max_px": 0.527},
        "checks": checks,
    }


def test_format_report_names_a_check_that_failed():
    report = report_fixture([
        {"name": "intrinsics match the published calibration", "ok": False,
         "detail": "cx off by 10.0 px"},
        {"name": "the board is flat", "ok": True, "detail": "0.79 mm"},
    ])

    text = chk.format_report(report)

    assert "intrinsics match the published calibration" in text
    assert "cx off by 10.0 px" in text
    # the verdict is the last thing, and it names how many checks failed
    assert text.rstrip().splitlines()[-1] == "FAIL (1 of 2 checks)"


def test_format_report_says_pass_when_every_check_passes():
    report = report_fixture([{"name": "everything", "ok": True, "detail": "fine"}])

    assert chk.format_report(report).rstrip().splitlines()[-1] == "PASS"


@needs_sample
def test_main_reports_success_on_the_sample_it_was_given():
    assert chk.main(["--cache-dir", str(SAMPLE_DIR), "--no-download"]) == 0


@needs_sample
def test_main_fails_when_a_tolerance_cannot_be_met():
    # no two calibrations agree to a part in a billion
    assert chk.main(["--cache-dir", str(SAMPLE_DIR), "--no-download",
                     "--rel-tol", "1e-9", "--abs-tol-px", "1e-9"]) == 1
