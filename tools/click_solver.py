"""One click set in, one measured point out, with no truth assumed.

This is the calibrated-camera half of Stage 2. It takes the three projection
matrices a calibration file publishes, the three clicked pixels, and the rig
transform, and returns exactly what a real system can know: the point, the
reprojection residuals, and the field verdict. The synthetic stream keeps its
own truth check; this module never invents one.

The arithmetic lives in ``targeting/triangulation.py``; this file only arranges
the inputs and packages the result for the log and the Slicer panel.
"""

from __future__ import annotations

import json
import math
import pathlib
from dataclasses import dataclass

import numpy as np

from targeting.camera_log import MAX_RESIDUAL_PX
from targeting.converter import OutOfFieldError, check_in_field_ras, ras_to_cube
from targeting.triangulation import project, triangulate


@dataclass(frozen=True)
class Measurement:
    """What a click set produced, or why it produced nothing."""

    status: str
    message: str = ""
    pixels: tuple = ()
    cube: tuple = ()
    ras: tuple = ()
    rig_point: tuple = ()
    residual_px: tuple = ()
    error_mm: float | None = None


@dataclass(frozen=True)
class Calibration:
    """The three camera projections and the pixel size they describe.

    ``intrinsics`` and ``distortions`` are optional because the synthetic rig
    publishes pinhole projections with neither. When a real calibration carries
    them, ``undistort`` is what makes the frame agree with those projections.
    """

    projections: tuple
    width: int
    height: int
    intrinsics: tuple = ()
    distortions: tuple = ()
    max_residual_px: float = MAX_RESIDUAL_PX
    demo: bool = False

    @property
    def needs_undistort(self) -> bool:
        return any(
            matrix is not None and coefficients is not None
            and np.any(np.abs(np.asarray(coefficients, float)) > 1e-12)
            for matrix, coefficients in zip(self.intrinsics, self.distortions)
        )

    def undistort(self, index, frame):
        """Remove the lens bend this camera's calibration describes."""
        if index >= len(self.intrinsics) or index >= len(self.distortions):
            return frame
        matrix = self.intrinsics[index]
        coefficients = self.distortions[index]
        if matrix is None or coefficients is None:
            return frame
        matrix = np.asarray(matrix, float)
        coefficients = np.asarray(coefficients, float)
        if matrix.shape != (3, 3) or coefficients.size == 0:
            return frame
        if np.all(np.abs(coefficients) <= 1e-12):
            return frame
        import cv2  # noqa: PLC0415 - only the calibrated path needs it

        return cv2.undistort(np.asarray(frame), matrix, coefficients)


def load_calibration(path) -> Calibration:
    """Read the same ``camera-rig.json`` the synthetic path publishes.

    The file is the contract, not a camera model: three 3x4 projection
    matrices (rig mm -> pixel), each with the frame size its pixels belong to.
    A malformed or incomplete file is refused whole; guessing a missing pose
    would produce a plausible wrong point.
    """
    path = pathlib.Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read the calibration at {path}: {exc}") from None
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from None
    cameras = payload.get("cameras") if isinstance(payload, dict) else None
    if not isinstance(cameras, list) or len(cameras) != 3:
        raise ValueError(f"{path}: expected three cameras, got "
                         f"{len(cameras) if isinstance(cameras, list) else 0}")
    projections = []
    intrinsics = []
    distortions = []
    has_intrinsics = any(isinstance(camera, dict) and camera.get("K") is not None
                         for camera in cameras)
    width = height = None
    for index, camera in enumerate(cameras):
        try:
            matrix = np.asarray(camera["projection"], dtype=float)
            camera_width = int(camera["width"])
            camera_height = int(camera["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: camera {index + 1}: {exc}") from None
        if matrix.shape != (3, 4) or not np.all(np.isfinite(matrix)):
            raise ValueError(
                f"{path}: camera {index + 1}: projection must be a finite 3x4 matrix")
        if camera_width <= 0 or camera_height <= 0:
            raise ValueError(f"{path}: camera {index + 1}: frame size must be positive")
        if width is None:
            width, height = camera_width, camera_height
        elif (camera_width, camera_height) != (width, height):
            raise ValueError(
                f"{path}: camera {index + 1} is {camera_width}x{camera_height}, "
                f"but camera 1 is {width}x{height}")
        projections.append(tuple(tuple(float(value) for value in row)
                                 for row in matrix))
        raw_K = camera.get("K")
        raw_distortion = camera.get("distortion")
        if raw_distortion is not None and raw_K is None:
            raise ValueError(
                f"{path}: camera {index + 1}: distortion without K")
        if has_intrinsics and raw_K is None:
            raise ValueError(
                f"{path}: camera {index + 1}: K is missing while other cameras have it")
        if raw_K is None:
            continue
        K = np.asarray(raw_K, dtype=float)
        if (K.shape != (3, 3) or not np.all(np.isfinite(K))
                or K[0, 0] == 0 or K[1, 1] == 0):
            raise ValueError(
                f"{path}: camera {index + 1}: K must be a finite 3x3 camera matrix")
        if raw_distortion is None:
            distortion = np.zeros(5)
        else:
            distortion = np.asarray(raw_distortion, dtype=float).ravel()
            if distortion.size < 4 or not np.all(np.isfinite(distortion)):
                raise ValueError(
                    f"{path}: camera {index + 1}: distortion must have at least 4 finite coefficients")
            if distortion.size == 4:
                distortion = np.append(distortion, 0.0)
        intrinsics.append(tuple(tuple(float(value) for value in row) for row in K))
        distortions.append(tuple(float(value) for value in distortion[:5]))
    raw_limit = payload.get("max_residual_px", MAX_RESIDUAL_PX)
    try:
        max_residual_px = float(raw_limit)
    except (TypeError, ValueError):
        raise ValueError(
            f"{path}: max_residual_px must be a number") from None
    if not math.isfinite(max_residual_px) or max_residual_px <= 0:
        raise ValueError(f"{path}: max_residual_px must be positive")
    return Calibration(
        projections=tuple(projections), width=width, height=height,
        intrinsics=tuple(intrinsics) if has_intrinsics else (),
        distortions=tuple(distortions) if has_intrinsics else (),
        max_residual_px=max_residual_px,
        demo=bool(payload.get("demo", False)),
    )


def solve_click_set(projections, pixels, rig, frame, truth=None,
                    max_residual_px=MAX_RESIDUAL_PX) -> Measurement:
    """Triangulate three pixels, check the field, and measure the residuals."""
    max_residual_px = float(max_residual_px)
    if not math.isfinite(max_residual_px) or max_residual_px <= 0:
        raise ValueError("max_residual_px must be positive")
    pixels = tuple((float(pixel[0]), float(pixel[1])) for pixel in pixels)
    if len(pixels) != 3:
        raise ValueError(f"a click set needs three pixels, got {len(pixels)}")
    projections = [projection for projection in projections]
    try:
        point = triangulate(projections, pixels)
    except ValueError as exc:
        return Measurement(status="degenerate", message=str(exc), pixels=pixels)
    ras = tuple(float(value) for value in rig.to_ras(point))
    try:
        check_in_field_ras(dict(zip(("R", "A", "S"), ras)), frame)
    except OutOfFieldError as exc:
        return Measurement(status="outside-field", message=str(exc), pixels=pixels,
                           ras=ras, rig_point=tuple(float(value) for value in point))
    residuals = []
    for projection, (u_px, v_px) in zip(projections, pixels):
        predicted_u, predicted_v = project(projection, point)
        residuals.append(float(math.hypot(predicted_u - u_px, predicted_v - v_px)))
    error = None
    if truth is not None:
        error = float(math.sqrt(sum(
            (float(point[index]) - float(truth[index])) ** 2 for index in range(3))))
    largest = max(residuals)
    if largest > max_residual_px:
        detail = ", ".join(f"{value:.2f}" for value in residuals)
        return Measurement(
            status="inconsistent",
            message=(f"click set inconsistent: reprojection residual "
                     f"{detail} px exceeds {max_residual_px:.2f} px; no point"),
            pixels=pixels,
            residual_px=tuple(residuals),
            error_mm=error,
        )
    return Measurement(
        status="found",
        pixels=pixels,
        cube=tuple(float(value) for value in ras_to_cube(ras, frame)),
        ras=ras,
        rig_point=tuple(float(value) for value in point),
        residual_px=tuple(residuals),
        error_mm=error,
    )
