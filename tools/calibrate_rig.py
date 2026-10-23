"""Calibrate three cameras from a printed checkerboard and write the rig file.

The stream consumes a ``camera-rig.json`` with three 3x4 projection matrices in
millimetres. This tool produces that file from photographs:

    captures/
      c1/pose00.png pose01.png ...
      c2/pose00.png pose01.png ...
      c3/pose00.png pose01.png ...

Every camera folder must contain the same pose names. Each camera's intrinsics
come from all of its readable boards; the rig extrinsics are averaged over every
pose that all three cameras saw. Millimetres are used throughout, matching the
rest of the project, so no metre-to-millimetre rescale can be forgotten.

The projection matrices describe *undistorted* pixels, using the ``K`` and
``distortion`` stored beside them. ``camera_stream.py`` applies that undistort
before sending frames when the coefficients are non-zero; a pinhole capture can
use ``--zero-distortion``.

The rig frame is camera 1's optical frame. Registering that frame to the
patient/field frame is a separate step; this tool deliberately does not invent
a transform it cannot see.

Usage:
    uv run tools/calibrate_rig.py --captures path/to/captures \
        --out path/to/camera-rig.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime

import cv2
import numpy as np


def _resolve_project_root(_globals=None, _env=None):
    """Find the checkout root without scanning the disk (same rule as the tools)."""
    import os

    g = _globals if _globals is not None else globals()
    e = _env if _env is not None else os.environ
    override = g.get("PROJECT_ROOT_OVERRIDE")
    if override:
        return pathlib.Path(override)
    env = e.get("HIMX_CAM_ROOT")
    if env:
        return pathlib.Path(env)
    try:
        here = pathlib.Path(__file__).resolve()
    except NameError:
        here = None
    if here is not None and here.is_file():
        for parent in [here.parent, *here.parents]:
            if (parent / "frame.json").is_file() and (
                parent / "targeting" / "converter.py"
            ).is_file():
                return parent
    raise RuntimeError(
        "could not locate himx-cam checkout (no fallback): set HIMX_CAM_ROOT "
        f"to the directory containing frame.json. __file__={here!r}")


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import stereo_board  # noqa: E402

CAMERA_NAMES = ("c1", "c2", "c3")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")
SUBPIXEL_CRITERIA = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
MIN_VIEWS = 3


def _gray(image):
    image = np.asarray(image)
    if image.ndim == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image


def detect_board(image, board):
    """Sub-pixel corners of a ``cols x rows`` inner-corner chessboard."""
    cols, rows = board
    gray = _gray(image)
    found, corners = cv2.findChessboardCorners(
        gray, (cols, rows),
        flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE,
    )
    if not found:
        raise ValueError(f"no {cols}x{rows} chessboard found")
    corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1),
                               SUBPIXEL_CRITERIA)
    return corners.reshape(-1, 2).astype(np.float64)


def _zero_distortion_flags():
    flags = (cv2.CALIB_FIX_ASPECT_RATIO | cv2.CALIB_ZERO_TANGENT_DIST
             | cv2.CALIB_FIX_K1 | cv2.CALIB_FIX_K2 | cv2.CALIB_FIX_K3)
    for name in ("CALIB_FIX_K4", "CALIB_FIX_K5", "CALIB_FIX_K6"):
        if hasattr(cv2, name):
            flags |= getattr(cv2, name)
    return flags


def calibrate_camera(corner_sets, board, square_mm, image_size,
                     zero_distortion=False):
    """OpenCV intrinsics from one camera's detected boards."""
    grid = stereo_board.object_points(board[0], board[1], square_mm).astype(np.float32)
    object_views = [grid for _ in corner_sets]
    pixel_views = [np.asarray(corners, np.float32).reshape(-1, 1, 2)
                   for corners in corner_sets]
    flags = (cv2.CALIB_FIX_ASPECT_RATIO | _zero_distortion_flags()
             if zero_distortion else cv2.CALIB_FIX_ASPECT_RATIO)
    rms, K, distortion, _rvecs, _tvecs = cv2.calibrateCamera(
        object_views, pixel_views, image_size, None, None, flags=flags)
    K = np.asarray(K, float)
    distortion = np.asarray(distortion, float).ravel()
    if distortion.size < 5:
        distortion = np.pad(distortion, (0, 5 - distortion.size))
    return {
        "K": K,
        "distortion": distortion[:5],
        "rms": float(rms),
        "views": len(corner_sets),
    }


def _solve_pose(corners, K, distortion, grid):
    """Board-to-camera pose for one view, in millimetres."""
    object_points = grid.reshape(-1, 1, 3).astype(np.float32)
    image_points = np.asarray(corners, np.float32).reshape(-1, 1, 2)
    for flag in (cv2.SOLVEPNP_IPPE, cv2.SOLVEPNP_ITERATIVE):
        ok, rvec, tvec = cv2.solvePnP(
            object_points, image_points, np.asarray(K, float),
            np.asarray(distortion, float), flags=flag)
        if ok:
            return (cv2.Rodrigues(rvec)[0],
                    np.asarray(tvec, float).reshape(3))
    raise ValueError("solvePnP could not place this board")


def average_rotation(rotations):
    """The nearest proper rotation to the mean of several rotations."""
    if not rotations:
        raise ValueError("no rotations to average")
    total = np.sum(np.asarray(rotations, float), axis=0)
    left, _singular, right = np.linalg.svd(total)
    rotation = left @ np.diag([1.0, 1.0, np.linalg.det(left @ right)]) @ right
    return rotation


def _relative_pose(poses, index):
    """Average camera-``index`` pose relative to camera 1, from common views."""
    R1, t1 = poses[0]
    Ri, ti = poses[index]
    R = Ri @ R1.T
    t = ti - R @ t1
    return R, t


def calibrate_rig(image_views, board, square_mm, image_size=None,
                  zero_distortion=False, names=CAMERA_NAMES):
    """Recover K and rig extrinsics from three aligned image lists.

    ``image_views[camera][pose]`` is one image. Detections that fail are left
    out of that camera's intrinsics; poses seen by all three are used for the
    rig, so a hand-held board that leaves one frame cannot silently move a
    camera.
    """
    if len(image_views) != len(names):
        raise ValueError(f"expected {len(names)} camera image lists, "
                         f"got {len(image_views)}")
    if len(board) != 2 or board[0] < 2 or board[1] < 2:
        raise ValueError("a board needs at least 2 by 2 inner corners")
    if square_mm <= 0:
        raise ValueError("the square size must be positive millimetres")
    counts = {len(images) for images in image_views}
    if len(counts) != 1:
        raise ValueError("every camera needs the same number of poses")
    pose_count = counts.pop()
    if pose_count < MIN_VIEWS:
        raise ValueError(f"need at least {MIN_VIEWS} poses per camera, got {pose_count}")

    detected = []
    for camera_index, images in enumerate(image_views):
        corners = []
        for pose_index, image in enumerate(images):
            if image is None:
                corners.append(None)
                continue
            try:
                corners.append(detect_board(image, board))
            except ValueError:
                corners.append(None)
        if sum(item is not None for item in corners) < MIN_VIEWS:
            raise ValueError(
                f"{names[camera_index]} has fewer than {MIN_VIEWS} readable boards")
        detected.append(corners)

    first_image = next(image for images in image_views for image in images
                       if image is not None)
    inferred_size = (int(_gray(first_image).shape[1]), int(_gray(first_image).shape[0]))
    if image_size is None:
        image_size = inferred_size
    image_size = (int(image_size[0]), int(image_size[1]))
    if image_size != inferred_size:
        raise ValueError(
            f"declared image size {image_size} does not match the images "
            f"{inferred_size}")
    for camera_index, images in enumerate(image_views):
        for pose_index, image in enumerate(images):
            if image is None:
                continue
            shape = _gray(image).shape[:2]
            if (shape[1], shape[0]) != image_size:
                raise ValueError(
                    f"{names[camera_index]} pose {pose_index + 1} is "
                    f"{shape[1]}x{shape[0]}, expected {image_size[0]}x{image_size[1]}")

    intrinsics = []
    for camera_index, corners in enumerate(detected):
        readable = [view for view in corners if view is not None]
        intrinsics.append(calibrate_camera(
            readable, board, square_mm, image_size,
            zero_distortion=zero_distortion))

    common = [pose for pose in range(pose_count)
              if all(corners[pose] is not None for corners in detected)]
    if len(common) < MIN_VIEWS:
        raise ValueError(
            f"need at least {MIN_VIEWS} poses seen by all three cameras, "
            f"got {len(common)}")

    grid = stereo_board.object_points(board[0], board[1], square_mm)
    relative = []
    for pose_index in common:
        poses = []
        for camera_index in range(len(names)):
            poses.append(_solve_pose(
                detected[camera_index][pose_index],
                intrinsics[camera_index]["K"],
                intrinsics[camera_index]["distortion"],
                grid,
            ))
        relative.append(poses)

    cameras = []
    for camera_index, name in enumerate(names):
        if camera_index == 0:
            R_rig = np.eye(3)
            t_rig = np.zeros(3)
        else:
            rotations = []
            translations = []
            for poses in relative:
                R, t = _relative_pose(poses, camera_index)
                rotations.append(R)
                translations.append(t)
            R_rig = average_rotation(rotations)
            t_rig = np.mean(np.asarray(translations, float), axis=0)
            if float(np.linalg.det(R_rig)) < 0:
                raise ValueError(f"{name}: recovered a mirrored rotation")
        K = intrinsics[camera_index]["K"]
        projection = K @ np.column_stack([R_rig, t_rig])
        cameras.append({
            "name": name,
            "projection": np.asarray(projection, float).tolist(),
            "K": np.asarray(K, float).tolist(),
            "distortion": np.asarray(intrinsics[camera_index]["distortion"],
                                     float).tolist(),
            "width": image_size[0],
            "height": image_size[1],
            "rms": intrinsics[camera_index]["rms"],
            "views": intrinsics[camera_index]["views"],
        })

    return {
        "when": datetime.now().isoformat(timespec="seconds"),
        "units": "mm",
        "rig_frame": "camera-1-optical",
        "board": {"cols": int(board[0]), "rows": int(board[1]),
                  "square_mm": float(square_mm)},
        "image_size": [image_size[0], image_size[1]],
        "zero_distortion": bool(zero_distortion),
        "cameras": cameras,
    }


def write_rig_file(result, path):
    """Write the calibration atomically, with no partial JSON for the stream."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def _image_names(folder):
    return sorted(path.name for path in folder.iterdir()
                  if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)


def load_captures(folder, names=CAMERA_NAMES):
    """Read matching pose files from ``folder/c1``, ``folder/c2``, ``folder/c3``."""
    folder = pathlib.Path(folder)
    images = []
    names_by_camera = []
    for name in names:
        camera_folder = folder / name
        if not camera_folder.is_dir():
            raise ValueError(f"{camera_folder} is missing; expected {name}/ "
                             "inside the capture folder")
        found = _image_names(camera_folder)
        if not found:
            raise ValueError(f"{camera_folder} has no images")
        names_by_camera.append(found)
    common = set(names_by_camera[0])
    for found in names_by_camera[1:]:
        common &= set(found)
    common = sorted(common)
    if len(common) < MIN_VIEWS:
        raise ValueError(
            f"need at least {MIN_VIEWS} matching pose names across all three "
            f"cameras, got {len(common)}")
    for name, found in zip(names, names_by_camera):
        for filename in common:
            image = cv2.imread(str(folder / name / filename),
                               cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError(f"cannot read {folder / name / filename}")
            images.append(image)
    image_views = [images[index * len(common):(index + 1) * len(common)]
                   for index in range(len(names))]
    height, width = image_views[0][0].shape[:2]
    return image_views, (width, height)


def calibrate_from_folders(folder, board, square_mm, zero_distortion=False,
                           image_size=None, names=CAMERA_NAMES):
    """The CLI path: read ``captures/c1`` ... and calibrate them."""
    image_views, inferred_size = load_captures(folder, names=names)
    return calibrate_rig(
        image_views, board=board, square_mm=square_mm,
        image_size=inferred_size if image_size is None else image_size,
        zero_distortion=zero_distortion, names=names)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--captures", required=True,
                        help="folder containing c1/, c2/, c3/ pose images")
    parser.add_argument("--out", required=True,
                        help="output camera-rig.json path")
    parser.add_argument("--board-cols", type=int, default=9,
                        help="inner corners along the board's columns")
    parser.add_argument("--board-rows", type=int, default=6,
                        help="inner corners along the board's rows")
    parser.add_argument("--square-mm", type=float, default=25.0,
                        help="checkerboard square size in millimetres")
    parser.add_argument("--zero-distortion", action="store_true",
                        help="fix distortion to zero (pinhole captures)")
    parser.add_argument("--image-size", default=None,
                        help="WIDTHxHEIGHT; inferred from the images when omitted")
    args = parser.parse_args(argv)

    image_size = None
    if args.image_size:
        try:
            width, height = args.image_size.lower().split("x")
            image_size = (int(width), int(height))
        except ValueError:
            print("--image-size must look like 640x480", file=sys.stderr)
            return 1
    try:
        result = calibrate_from_folders(
            args.captures, board=(args.board_cols, args.board_rows),
            square_mm=args.square_mm, zero_distortion=args.zero_distortion,
            image_size=image_size)
        path = write_rig_file(result, args.out)
    except (ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1

    for camera in result["cameras"]:
        K = np.asarray(camera["K"], float)
        print(f"{camera['name']}: fx={K[0, 0]:.2f} fy={K[1, 1]:.2f} "
              f"cx={K[0, 2]:.2f} cy={K[1, 2]:.2f} "
              f"rms={camera['rms']:.3f} px views={camera['views']}", flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
