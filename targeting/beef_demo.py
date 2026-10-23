"""A clearly-marked demo calibration for the Neural 3D Video beef clips.

The footage has no checkerboard. Its ``poses_bounds.npy`` still gives the fixed
camera poses in normalized LLFF/NeRF units, so the click-to-3D flow can be
demonstrated after choosing an estimated scale. This module converts the three
selected cameras to 320-pixel projections, centres the scene on the 100 mm
field, and writes the same ``camera-rig.json`` shape the real calibrator uses.

It is not a measurement calibration: the scale is a convention, the lens
distortion is not modelled, and the result must stay marked as demo-only.
"""

from __future__ import annotations

import json
import math
import pathlib
from datetime import datetime

import numpy as np

from targeting.triangulation import triangulate

FULL_WIDTH = 2704
FULL_HEIGHT = 2028
DEMO_WIDTH = 320
DEMO_HEIGHT = 320
DEMO_SCALE_MM_PER_UNIT = 4.0
DEMO_MAX_RESIDUAL_PX = 20.0
FILTERED_CAMERA = 4
CAMERA_IDS = (0, 11, 20)


class BeefDemoError(ValueError):
    """The beef demo calibration cannot be built."""


def pose_row_for_camera(camera_id, filtered_camera=FILTERED_CAMERA):
    """The row in the 20-pose file for one valid camera id.

    The Neural 3D Video release has camera ids 0..20 but one stream was
    filtered. The file stores the remaining ids in sorted order, so a camera
    after the filtered id shifts down by one.
    """
    camera_id = int(camera_id)
    if filtered_camera is not None and camera_id == int(filtered_camera):
        raise BeefDemoError(
            f"camera {camera_id} is the filtered camera and has no pose")
    if filtered_camera is None or camera_id < int(filtered_camera):
        row = camera_id
    else:
        row = camera_id - 1
    if row < 0:
        raise BeefDemoError(f"camera {camera_id} has no pose row")
    return row


def load_poses(path):
    """Read the LLFF pose file as an ``(N, 3, 5)`` array."""
    path = pathlib.Path(path)
    try:
        raw = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise BeefDemoError(f"cannot read the poses at {path}: {exc}") from None
    if raw.ndim != 2 or raw.shape[1] < 15:
        raise BeefDemoError(
            f"{path}: expected an (N, 17) LLFF pose file, got {raw.shape}")
    return raw[:, :15].reshape(-1, 3, 5)


def projection_from_pose(pose, *, width=DEMO_WIDTH, height=DEMO_HEIGHT,
                         full_width=FULL_WIDTH, full_height=FULL_HEIGHT):
    """A 320-pixel projection matrix from one LLFF camera-to-world pose.

    The raw focal is for the 2704x2028 frame. The 320 clips are resized to a
    square, so fx and fy are different; that is why the projection is built
    explicitly rather than by scaling one focal.
    """
    pose = np.asarray(pose, dtype=float)
    if pose.shape != (3, 5) or not np.all(np.isfinite(pose)):
        raise BeefDemoError("a pose must be a finite 3x5 matrix")
    rotation = pose[:3, :3]
    translation = pose[:3, 3]
    focal = float(pose[0, 4])
    if focal <= 0:
        raise BeefDemoError("the pose focal length must be positive")
    world_to_camera = np.eye(4)
    world_to_camera[:3, :3] = rotation.T
    world_to_camera[:3, 3] = -rotation.T @ translation
    intrinsics = np.array([
        [focal * width / full_width, 0.0, width / 2.0],
        [0.0, focal * height / full_height, height / 2.0],
        [0.0, 0.0, 1.0],
    ])
    return intrinsics @ world_to_camera[:3, :]


def demo_projection(projection, anchor, scale_mm_per_unit):
    """Projection for ``Y = scale * (X - anchor)`` demo millimetres."""
    scale = float(scale_mm_per_unit)
    if not math.isfinite(scale) or scale <= 0:
        raise BeefDemoError("the demo scale must be positive")
    anchor = np.asarray(anchor, dtype=float)
    if anchor.shape != (3,) or not np.all(np.isfinite(anchor)):
        raise BeefDemoError("the demo anchor must be three finite numbers")
    projection = np.asarray(projection, dtype=float)
    if projection.shape != (3, 4) or not np.all(np.isfinite(projection)):
        raise BeefDemoError("the projection must be a finite 3x4 matrix")
    scaled = np.empty((3, 4), dtype=float)
    scaled[:, :3] = projection[:, :3] / scale
    scaled[:, 3] = projection[:, 3] + projection[:, :3] @ anchor
    return scaled


def write_demo_calibration(poses_path, out_path, *,
                           anchor=None,
                           scale_mm_per_unit=DEMO_SCALE_MM_PER_UNIT,
                           max_residual_px=DEMO_MAX_RESIDUAL_PX,
                           filtered_camera=FILTERED_CAMERA,
                           camera_ids=CAMERA_IDS):
    """Write the demo camera-rig JSON and return its payload."""
    poses = load_poses(poses_path)
    selected = []
    for camera_id in camera_ids:
        row = pose_row_for_camera(camera_id, filtered_camera)
        if row >= len(poses):
            raise BeefDemoError(
                f"camera {camera_id} wants pose row {row}, but the file has "
                f"only {len(poses)} rows")
        selected.append((camera_id, row))

    projections = [projection_from_pose(poses[row]) for _camera_id, row in selected]
    if anchor is None:
        try:
            anchor = triangulate(
                projections, [(DEMO_WIDTH / 2.0, DEMO_HEIGHT / 2.0)] * 3)
        except ValueError as exc:
            raise BeefDemoError(
                f"cannot locate a demo anchor from the three camera centres: {exc}"
            ) from None
    anchor = tuple(float(value) for value in anchor)
    if len(anchor) != 3 or not all(math.isfinite(value) for value in anchor):
        raise BeefDemoError("the demo anchor is not three finite numbers")
    if not math.isfinite(float(max_residual_px)) or float(max_residual_px) <= 0:
        raise BeefDemoError("max_residual_px must be positive")

    cameras = []
    for (camera_id, row), projection in zip(selected, projections):
        demo = demo_projection(projection, anchor, scale_mm_per_unit)
        cameras.append({
            "name": f"cam{camera_id:02d}",
            "projection": demo.tolist(),
            "width": DEMO_WIDTH,
            "height": DEMO_HEIGHT,
        })

    payload = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "demo": True,
        "estimated": True,
        "source": "Neural 3D Video cut_roasted_beef poses_bounds.npy",
        "camera_ids": [int(camera_id) for camera_id, _row in selected],
        "pose_rows": [int(row) for _camera_id, row in selected],
        "scale_mm_per_unit": float(scale_mm_per_unit),
        "anchor_llff": list(anchor),
        "max_residual_px": float(max_residual_px),
        "warning": (
            "DEMO ONLY: estimated scale from normalized LLFF poses; lens "
            "distortion and metric scale are not calibrated. Do not use for "
            "a measurement."),
        "cameras": cameras,
    }
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(out_path)
    return payload
