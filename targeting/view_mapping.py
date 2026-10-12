"""Derive per-view pixel constants from Slicer's own view matrices.

Slicer's slice node owns the mapping from viewport pixels to RAS, and the
header of vtkMRMLSliceNode documents GetXYToRAS() as exactly that:

    "Matrix mapping from XY pixel coordinates on an image window into RAS
     world coordinates"

That matrix already contains the current field of view, the window size and
the view orientation, and Slicer recomputes it whenever any of those change
(loading a volume, zooming, resizing, moving to another screen). It is the
same mapping Slicer uses to render the view and to place the Data Probe
crosshair under the mouse, so reading it live replaces every stored constant.
For the Red view, a mouse 60.5 px above centre at 250mm/137px reports
A = +110.4, which is this mapping applied to a display position.

Conventions encoded here:
  * screen-right = +column 0 of XYToRAS;
  * screen-up    = +column 1, so screen-down (the converter's v) = -column 1;
  * the slice offset slider moves the plane along +column 2, so the depth
    sign is the dominant sign of column 2;
  * XY pixel coordinates have their origin at the view's bottom-left and y
    pointing up, hence the flip when the centre is expressed in Qt pixels
    (top-left origin, y down). The flip carries a -1: Qt rows count 0..H-1
    and Slicer converts them with the equivalent of VTK's
    SetEventPositionFlipY (H-1-y). Without the -1 every conversion is one
    pixel (about 1.4-1.8 mm) high.

The stored frame.json keeps only the physical field and the last verified
axis directions, as documentation and a sanity reference. The live values
always win.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, Tuple

from .frame import Frame

_AXES = ("R", "A", "S")

# How far the slice origin may sit off the view plane, in mm, before the
# matrix is treated as corrupt rather than floating-point noise.
_OFF_PLANE_TOLERANCE_MM = 1e-6

# Relative slack for "the same scale on both axes".
_ISOTROPY_TOLERANCE = 1e-3


class ViewMatrixError(ValueError):
    """Raised when a view matrix cannot be turned into view constants."""


class NonIsotropicView(ViewMatrixError):
    """Raised when pixels are not square, or the display scale differs per axis."""


class DegenerateView(ViewMatrixError):
    """Raised when the matrix does not span a proper 2D plane."""


@dataclass(frozen=True)
class ViewConstants:
    """Everything a view contributes to the touch mapping, in Qt pixel space."""

    mm_per_px: float
    center_px: Tuple[float, float]
    axis_map: Dict[str, Tuple[str, int]]
    origin_xy_px: Tuple[float, float]  # slice origin in matrix (device) pixels
    display_scale: float  # matrix pixels per Qt logical pixel


def _column(matrix: Sequence[Sequence[float]], index: int) -> List[float]:
    return [float(matrix[row][index]) for row in range(3)]


def _length(vector: Sequence[float]) -> float:
    return (vector[0] ** 2 + vector[1] ** 2 + vector[2] ** 2) ** 0.5


def _dominant(vector: Sequence[float], what: str) -> Tuple[str, int]:
    best = max(range(3), key=lambda i: abs(vector[i]))
    if abs(vector[best]) <= 1e-12:
        raise DegenerateView(f"{what} has no RAS direction: {list(vector)}")
    return _AXES[best], (1 if vector[best] > 0.0 else -1)


def _solve_in_plane(
    matrix: Sequence[Sequence[float]], rhs: Sequence[float]
) -> Tuple[float, float, Tuple[int, int]]:
    """Solve M * (x, y, 0, 1) = rhs for the XY pixel coordinates.

    Uses the row pair with the largest determinant, so it also works for the
    sagittal and coronal views, where the first matrix row carries no R
    information at all.
    """
    best = None
    for i, j in itertools.combinations(range(3), 2):
        a, b = float(matrix[i][0]), float(matrix[i][1])
        c, d = float(matrix[j][0]), float(matrix[j][1])
        det = a * d - b * c
        if best is None or abs(det) > abs(best[0]):
            best = (det, i, j, a, b, c, d)
    det, i, j, a, b, c, d = best
    scale = max(abs(a), abs(b), abs(c), abs(d), 1.0)
    if abs(det) <= 1e-9 * scale * scale:
        raise DegenerateView("the view matrix does not span a 2D plane")
    e, f = float(rhs[i]), float(rhs[j])
    x = (e * d - b * f) / det
    y = (a * f - e * c) / det
    return x, y, (i, j)


def constants_for_view(view_data: Mapping) -> ViewConstants:
    """Turn one view's live matrix into the constants the converter needs.

    `view_data` keys: xy_to_ras (4x4, row-major), slice_origin_ras (3),
    width_px, height_px and optionally qt_width_px, qt_height_px for HiDPI.
    """
    matrix = view_data["xy_to_ras"]
    width = float(view_data["width_px"])
    height = float(view_data["height_px"])
    if width <= 0.0 or height <= 0.0:
        raise ViewMatrixError(f"viewport must be positive, got {width}x{height}")

    col0 = _column(matrix, 0)
    col1 = _column(matrix, 1)
    col2 = _column(matrix, 2)

    scale_u, scale_v = _length(col0), _length(col1)
    if scale_u <= 0.0 or scale_v <= 0.0:
        raise DegenerateView("an in-plane column is zero-length")
    if abs(scale_u - scale_v) > _ISOTROPY_TOLERANCE * max(scale_u, scale_v):
        raise NonIsotropicView(
            f"pixels are not square: {scale_u:.6f} vs {scale_v:.6f} mm/px"
        )

    qt_width = float(view_data.get("qt_width_px", width))
    qt_height = float(view_data.get("qt_height_px", height))
    if qt_width <= 0.0 or qt_height <= 0.0:
        raise ViewMatrixError("Qt viewport must be positive")
    display_scale_x = width / qt_width
    display_scale_y = height / qt_height
    if abs(display_scale_x - display_scale_y) > _ISOTROPY_TOLERANCE * max(
        display_scale_x, display_scale_y
    ):
        raise NonIsotropicView(
            "display scale differs per axis: "
            f"{display_scale_x:.4f} vs {display_scale_y:.4f}"
        )
    display_scale = display_scale_x

    u_axis, u_sign = _dominant(col0, "screen-right")
    # XY y points up the screen; the converter counts v downward.
    v_axis, v_sign = _dominant([-c for c in col1], "screen-down")
    depth_axis, depth_sign = _dominant(col2, "slice normal")

    origin = [float(c) for c in view_data["slice_origin_ras"]]
    translation = [float(matrix[row][3]) for row in range(3)]
    ox, oy, rows_used = _solve_in_plane(
        matrix, [origin[i] - translation[i] for i in range(3)]
    )
    leftover = ({0, 1, 2} - set(rows_used)).pop()
    residual = (
        matrix[leftover][0] * ox
        + matrix[leftover][1] * oy
        + translation[leftover]
        - origin[leftover]
    )
    if abs(residual) > _OFF_PLANE_TOLERANCE_MM:
        raise DegenerateView(f"slice origin sits {residual:.6f}mm off the view plane")

    # Qt pixels are top-left, y down; matrix pixels are bottom-left, y up.
    # The -1 is the pixel-index flip: Qt rows run 0..H-1 and Slicer converts
    # them with VTK's H-1-y convention.
    center_matrix = (ox, (height - 1.0) - oy)
    center = (center_matrix[0] / display_scale, center_matrix[1] / display_scale)

    return ViewConstants(
        mm_per_px=scale_u * display_scale,
        center_px=center,
        axis_map={
            "u": (u_axis, u_sign),
            "v": (v_axis, v_sign),
            "depth": (depth_axis, depth_sign),
        },
        origin_xy_px=(ox, oy),
        display_scale=display_scale,
    )


def frame_from_views(base: Frame, view_data: Mapping[str, Mapping]) -> Frame:
    """A copy of `base` with all pixel geometry replaced by live values.

    The stored axis maps are kept for views the live data does not cover, but
    pixel_size_mm and view_center_px only exist for views that were actually
    read. A view with no live geometry raises MissingViewportError in the
    converter rather than silently converting.
    """
    pixel_size: Dict[str, float] = {}
    centers: Dict[str, Tuple[float, float]] = {}
    axis_maps = {view: dict(binding) for view, binding in base.view_axis_map.items()}
    for view, data in view_data.items():
        constants = constants_for_view(data)
        pixel_size[view] = constants.mm_per_px
        centers[view] = constants.center_px
        axis_maps[view] = dict(constants.axis_map)
    return Frame(
        field_size_mm=dict(base.field_size_mm),
        field_center_ras=dict(base.field_center_ras),
        pixel_size_mm=pixel_size,
        view_axis_map=axis_maps,
        view_center_px=centers,
    )
