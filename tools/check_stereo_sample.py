"""Check our camera work against OpenCV's own stereo sample.

OpenCV ships 13 stereo pairs of a 9x6 chessboard on a 25 mm grid together with
the intrinsics it recovered from them (``samples/data/left_intrinsics.yml``).
That turns the sample into free, real-image ground truth: no camera is needed,
and a wrong calibration or a wrong triangulation shows up as a number that
disagrees with the published one.

The geometry lives in :mod:`stereo_board`; this module is only the OpenCV
layer -- reading the answer key, finding boards, calibrating and triangulating.

Usage:
    python tools/check_stereo_sample.py [--cache-dir data/opencv-stereo-sample]

Exits non-zero if any check fails.
"""

import argparse
import pathlib

import cv2
import numpy as np

import stereo_board

SUBPIXEL_CRITERIA = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)


def load_reference_intrinsics(path):
    """OpenCV's published intrinsics for the sample, read from its own YAML.

    Raises rather than returning empty matrices if the file is not there: a
    silently unread reference would turn every later comparison into a pass.
    """
    path = pathlib.Path(path)
    storage = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
    if not storage.isOpened():
        raise RuntimeError(f"cannot read the reference intrinsics at {path}")
    try:
        matrix = storage.getNode("camera_matrix").mat()
        distortion = storage.getNode("distortion_coefficients").mat()
        reference = {
            "frames": int(storage.getNode("nframes").real()),
            "image_size": (
                int(storage.getNode("image_width").real()),
                int(storage.getNode("image_height").real()),
            ),
            "board": (
                int(storage.getNode("board_width").real()),
                int(storage.getNode("board_height").real()),
            ),
            "square_size": float(storage.getNode("square_size").real()),
            "rms": float(storage.getNode("avg_reprojection_error").real()),
            "K": np.asarray(matrix, dtype=float),
            "distortion": np.asarray(distortion, dtype=float).ravel(),
        }
    finally:
        storage.release()

    reference["fx"] = float(matrix[0, 0])
    reference["fy"] = float(matrix[1, 1])
    reference["cx"] = float(matrix[0, 2])
    reference["cy"] = float(matrix[1, 2])
    return reference


def detect_board(image, board):
    """Corner pixels of the board in ``image``, ordered as ``object_points`` is.

    Refined to sub-pixel positions before the calibration ever sees them.
    """
    cols, rows = board
    found, corners = cv2.findChessboardCorners(image, (cols, rows), None)
    if not found:
        raise RuntimeError(f"no {cols}x{rows} chessboard found in the image")
    corners = cv2.cornerSubPix(image, corners, (11, 11), (-1, -1), SUBPIXEL_CRITERIA)
    return corners.reshape(-1, 2)


def calibrate_monocular(image_points, board, square, image_size):
    """Camera matrix, distortion and RMS reprojection error from board views.

    The aspect ratio is fixed, mirroring the flags OpenCV used for the sample,
    so that a comparison against the published numbers is like for like.
    """
    grid = stereo_board.object_points(board[0], board[1], square).astype(np.float32)
    object_views = [grid] * len(image_points)
    pixel_views = [np.asarray(points, dtype=np.float32).reshape(-1, 1, 2) for points in image_points]

    rms, K, distortion, rvecs, tvecs = cv2.calibrateCamera(
        object_views, pixel_views, image_size, None, None,
        flags=cv2.CALIB_FIX_ASPECT_RATIO,
    )
    K = np.asarray(K, dtype=float)
    return {
        "rms": float(rms),
        "K": K,
        "distortion": np.asarray(distortion, dtype=float).ravel(),
        "fx": float(K[0, 0]),
        "fy": float(K[1, 1]),
        "cx": float(K[0, 2]),
        "cy": float(K[1, 2]),
        "rvecs": rvecs,
        "tvecs": tvecs,
    }


def calibrate_stereo(left_points, right_points, mono_left, mono_right, board, square, image_size):
    """Rotation and translation from the left camera to the right one.

    The two cameras' intrinsics are held fixed at what their own calibrations
    found, so a poor single-camera calibration cannot be papered over by the
    stereo step -- it shows up in the residuals instead.
    """
    grid = stereo_board.object_points(board[0], board[1], square).astype(np.float32)
    object_views = [grid] * len(left_points)
    left_pixels = [np.asarray(p, dtype=np.float32).reshape(-1, 1, 2) for p in left_points]
    right_pixels = [np.asarray(p, dtype=np.float32).reshape(-1, 1, 2) for p in right_points]

    rms, K1, d1, K2, d2, R, T, E, F = cv2.stereoCalibrate(
        object_views, left_pixels, right_pixels,
        mono_left["K"], mono_left["distortion"], mono_right["K"], mono_right["distortion"],
        image_size,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6),
        flags=cv2.CALIB_FIX_INTRINSIC,
    )
    R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(
        K1, d1, K2, d2, image_size, R, T, alpha=0.0,
    )
    return {
        "rms": float(rms),
        "K1": np.asarray(K1, dtype=float), "d1": np.asarray(d1, dtype=float).ravel(),
        "K2": np.asarray(K2, dtype=float), "d2": np.asarray(d2, dtype=float).ravel(),
        "R": np.asarray(R, dtype=float), "T": np.asarray(T, dtype=float),
        "E": np.asarray(E, dtype=float), "F": np.asarray(F, dtype=float),
        "R1": np.asarray(R1, dtype=float), "R2": np.asarray(R2, dtype=float),
        "P1": np.asarray(P1, dtype=float), "P2": np.asarray(P2, dtype=float),
        "Q": np.asarray(Q, dtype=float),
    }


def triangulate_points(projection_left, projection_right, points_left, points_right):
    """Where the two views' lines of sight meet, in the left camera's frame."""
    left = np.asarray(points_left, dtype=np.float64).reshape(-1, 2).T
    right = np.asarray(points_right, dtype=np.float64).reshape(-1, 2).T
    homogeneous = cv2.triangulatePoints(
        np.asarray(projection_left, dtype=np.float64),
        np.asarray(projection_right, dtype=np.float64),
        left, right,
    )
    return (homogeneous[:3] / homogeneous[3]).T


def rectified_points(points, K, distortion, R_rect, P_rect):
    """Where ``points`` land in a camera's rectified image.

    Triangulation from a rectified pair has to start here: the projection
    matrices ``stereoRectify`` hands back describe the straightened image, so
    measured pixels fed to them straight from the detector would carry the
    lens's bend into the geometry.
    """
    return cv2.undistortPoints(
        np.asarray(points, dtype=np.float32).reshape(-1, 1, 2),
        np.asarray(K, dtype=float), np.asarray(distortion, dtype=float),
        R=np.asarray(R_rect, dtype=float), P=np.asarray(P_rect, dtype=float),
    ).reshape(-1, 2)


def rectified_vertical_residuals(points_left, points_right, stereo):
    """How far the two views disagree vertically once rectified.

    A stereo pair that is geometrically consistent lines up row for row, so
    these should be a fraction of a pixel; anything larger means the pair's
    rotation, baseline or intrinsics are wrong.
    """
    left = rectified_points(points_left, stereo["K1"], stereo["d1"], stereo["R1"], stereo["P1"])
    right = rectified_points(points_right, stereo["K2"], stereo["d2"], stereo["R2"], stereo["P2"])
    return np.abs(left[:, 1] - right[:, 1])


def load_views(cache_dir, side, indices=stereo_board.SAMPLE_IMAGE_INDICES,
               board=(stereo_board.BOARD_COLS, stereo_board.BOARD_ROWS)):
    """Detected board corners in every ``side`` image of the cached sample."""
    cache_dir = pathlib.Path(cache_dir)
    views = []
    for index in indices:
        path = cache_dir / f"{side}{index:02d}.jpg"
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise RuntimeError(f"cannot read {path}")
        try:
            views.append(detect_board(image, board))
        except RuntimeError as error:
            raise RuntimeError(f"{path.name}: {error}") from error
    return views


def build_report(cache_dir, rel_tol=stereo_board.DEFAULT_REL_TOL,
                 abs_tol_px=stereo_board.DEFAULT_ABS_TOL_PX):
    """Measure the sample and judge every measurement, all in one place."""
    cache_dir = pathlib.Path(cache_dir)
    reference_path = cache_dir / "left_intrinsics.yml"
    reference = load_reference_intrinsics(reference_path)
    board, square = reference["board"], reference["square_size"]
    image_size = reference["image_size"]

    left_views = load_views(cache_dir, "left", board=board)
    right_views = load_views(cache_dir, "right", board=board)
    left = calibrate_monocular(left_views, board, square, image_size)
    right = calibrate_monocular(right_views, board, square, image_size)
    stereo = calibrate_stereo(left_views, right_views, left, right, board, square, image_size)

    points = triangulate_points(
        stereo["P1"], stereo["P2"],
        rectified_points(left_views[0], stereo["K1"], stereo["d1"], stereo["R1"], stereo["P1"]),
        rectified_points(right_views[0], stereo["K2"], stereo["d2"], stereo["R2"], stereo["P2"]),
    )
    centroid, normal = stereo_board.fit_plane(points)
    sides = stereo_board.grid_side_lengths(points, *board)
    flatness = stereo_board.planar_residuals(points, centroid, normal)
    rows = rectified_vertical_residuals(left_views[0], right_views[0], stereo)

    measurements = {
        "depth_mm": float(points[:, 2].mean() * 1000),
        "baseline_mm": float(np.linalg.norm(stereo["T"]) * 1000),
        "square_mean_mm": float(sides.mean() * 1000),
        "square_std_mm": float(sides.std() * 1000),
        "flatness_mean_mm": float(flatness.mean() * 1000),
        "flatness_median_mm": float(np.median(flatness) * 1000),
        "row_median_px": float(np.median(rows)),
        "row_max_px": float(rows.max()),
    }

    disagreements = stereo_board.compare_intrinsics(reference, left, rel_tol, abs_tol_px)
    intrinsics_ok = not disagreements
    intrinsics_detail = (
        "within tolerance"
        if intrinsics_ok
        else "; ".join(f"{row['name']} off by {row['abs_error']:.2f} px" for row in disagreements)
    )

    checks = [
        {
            "name": "camera intrinsics match OpenCV's published calibration",
            "ok": intrinsics_ok,
            "detail": intrinsics_detail,
        },
        {
            "name": "reprojection error is in the same league as the published one",
            "ok": left["rms"] < 1.5 * reference["rms"],
            "detail": f"{left['rms']:.4f} px against {reference['rms']:.4f} px",
        },
        {
            "name": "the reconstructed board is a rigid grid of one-square steps",
            "ok": abs(measurements["square_mean_mm"] - square * 1000) <= 0.01 * square * 1000
            and measurements["square_std_mm"] < 0.1 * square * 1000,
            "detail": f"mean {measurements['square_mean_mm']:.3f} mm, sd "
                      f"{measurements['square_std_mm']:.3f} mm, true {square * 1000:.3f} mm",
        },
        {
            "name": "the reconstructed board is flat",
            "ok": measurements["flatness_mean_mm"] < 0.05 * square * 1000,
            "detail": f"mean {measurements['flatness_mean_mm']:.3f} mm, median "
                      f"{measurements['flatness_median_mm']:.3f} mm",
        },
        {
            "name": "the rectified pair lines up row for row",
            "ok": measurements["row_median_px"] < 0.5 and measurements["row_max_px"] < 2.0,
            "detail": f"median {measurements['row_median_px']:.3f} px, "
                      f"max {measurements['row_max_px']:.3f} px",
        },
    ]
    return {
        "cache_dir": str(cache_dir),
        "reference": reference,
        "mono": left,
        "stereo": stereo,
        "measurements": measurements,
        "checks": checks,
    }


def format_report(report):
    """The report a human reads: the numbers, then each verdict, then a summary."""
    reference = report["reference"]
    left = report["mono"]
    measurements = report["measurements"]

    lines = [
        f"OpenCV stereo sample in {report['cache_dir']}",
        f"  {reference['frames']} pairs, {reference['board'][0]}x{reference['board'][1]} board, "
        f"{reference['square_size'] * 1000:.1f} mm squares",
        "",
        "Calibration (left camera)",
        f"  {'':6}{'published':>12}{'ours':>12}{'difference':>16}",
    ]
    for name in ("fx", "fy", "cx", "cy"):
        published, ours = reference[name], left[name]
        difference = ours - published
        lines.append(
            f"  {name:6}{published:12.3f}{ours:12.3f}"
            f"{difference:+11.3f} px{100 * difference / published:+8.4f} %"
        )
    lines.append(
        f"  {'rms':6}{reference['rms']:12.4f}{left['rms']:12.4f}"
        f"{left['rms'] - reference['rms']:+11.4f} px"
    )
    lines.append(
        f"  distortion  published {np.array2string(reference['distortion'], precision=5)}"
    )
    lines.append(
        f"              ours      {np.array2string(left['distortion'], precision=5)}"
    )

    lines += [
        "",
        "Reconstruction of the first pair",
        f"  working distance        {measurements['depth_mm']:.1f} mm",
        f"  baseline                {measurements['baseline_mm']:.1f} mm",
        f"  square side             {measurements['square_mean_mm']:.3f} mm "
        f"(true {reference['square_size'] * 1000:.3f} mm), "
        f"sd {measurements['square_std_mm']:.3f} mm",
        f"  board flatness          {measurements['flatness_mean_mm']:.3f} mm mean, "
        f"{measurements['flatness_median_mm']:.3f} mm median",
        f"  rectified rows          {measurements['row_median_px']:.3f} px median, "
        f"{measurements['row_max_px']:.3f} px max",
        "",
        "Checks",
    ]
    for check in report["checks"]:
        lines.append(f"  {'OK  ' if check['ok'] else 'FAIL'}  {check['name']}")
        lines.append(f"        {check['detail']}")

    failures = [check for check in report["checks"] if not check["ok"]]
    lines.append("")
    lines.append(f"FAIL ({len(failures)} of {len(report['checks'])} checks)" if failures else "PASS")
    return "\n".join(lines)


def main(argv=None):
    """Fetch the sample if it is not there yet, then print the report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache-dir", default="data/opencv-stereo-sample",
                        help="where the sample lives, and is fetched to if missing")
    parser.add_argument("--no-download", action="store_true",
                        help="fail rather than reach out for anything missing")
    parser.add_argument("--rel-tol", type=float, default=stereo_board.DEFAULT_REL_TOL,
                       help="how far a focal length may drift, as a proportion")
    parser.add_argument("--abs-tol-px", type=float, default=stereo_board.DEFAULT_ABS_TOL_PX,
                       help="how far a principal point may drift, in pixels")
    args = parser.parse_args(argv)

    cache_dir = pathlib.Path(args.cache_dir)
    if not (cache_dir / "left_intrinsics.yml").exists():
        if args.no_download:
            raise SystemExit(f"nothing in {cache_dir} and --no-download was given")
        stereo_board.download_sample(cache_dir)

    report = build_report(cache_dir, rel_tol=args.rel_tol, abs_tol_px=args.abs_tol_px)
    print(format_report(report))
    return 0 if all(check["ok"] for check in report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
