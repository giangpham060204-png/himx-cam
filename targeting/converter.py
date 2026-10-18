"""Convert a 2D touch plus a slice offset into a 3D point in the Cube frame.

Pure arithmetic: no screen, no Slicer, no clock, no file writes. Same inputs
always give the same output, on any machine.

Pixel coordinates are measured from the view CENTRE, because Slicer centres a
slice view on the slice origin. Pixel (u,v) = (0,0) is the field centre.
"""

from __future__ import annotations

from .frame import RAS_AXES, Frame

TOLERANCE_MM = 1e-6


class UnknownViewError(KeyError):
    """Raised when a view name is not one of XY, YZ, XZ."""


class MissingViewportError(RuntimeError):
    """Raised when a frame has no live pixel geometry for the requested view.

    Pixel geometry is not stored any more; it is read from the live Slicer
    views by `slicer_bridge.live_frame()`. A frame loaded straight from
    frame.json therefore cannot convert until that has happened.
    """


class OutOfFieldError(ValueError):
    """Raised when the touch or depth falls outside the declared field."""


def _check_in_field(value_mm, axis, low, high):
    if not (low - TOLERANCE_MM <= value_mm <= high + TOLERANCE_MM):
        raise OutOfFieldError(
            f"{axis}={value_mm:.3f}mm is outside the field {low:.3f}..{high:.3f}mm"
        )


def check_in_field_ras(ras, frame: Frame) -> None:
    """Raise OutOfFieldError unless this RAS point is inside the declared field.

    The camera path arrives with a point, not with a view and a pixel, so it
    needs the same rule without the pixel arithmetic around it. It doubles as
    the unit check: a row mistakenly written in metres is caught by the same
    bounds, so no separate unit heuristic is needed.
    """
    center = frame.field_center_ras
    half = {axis: frame.field_size_mm[axis] / 2.0 for axis in RAS_AXES}
    for axis in RAS_AXES:
        _check_in_field(ras[axis], axis, center[axis] - half[axis], center[axis] + half[axis])


def convert(view: str, u_px: float, v_px: float, depth_mm: float, frame: Frame):
    """Return the Cube-frame (x, y, z) in mm for a touch in `view`.

    u_px and v_px are view-local pixels measured RIGHT and DOWN from the view
    centre. depth_mm is the slice-offset slider value, measured from the RAS
    origin.
    """
    binding = frame.view_axis_map.get(view)
    if binding is None:
        raise UnknownViewError(f"no axis map for view {view!r}")
    if view not in frame.pixel_size_mm or view not in frame.view_center_px:
        raise MissingViewportError(
            f"view {view!r} has no live pixel geometry yet; build the frame "
            "with slicer_bridge.live_frame() (or fill pixel_size_mm and "
            "view_center_px)"
        )
    mm_per_px = frame.pixel_size_mm[view]
    centre_u, centre_v = frame.view_center_px[view]

    center = frame.field_center_ras
    half = {axis: frame.field_size_mm[axis] / 2.0 for axis in RAS_AXES}

    ras = {}
    for name, offset_px in (
        ("u", u_px - centre_u),
        ("v", v_px - centre_v),
    ):
        axis, sign = binding[name]
        ras[axis] = center[axis] + sign * offset_px * mm_per_px

    # The slice slider is already measured from the RAS origin, so no shift.
    axis, sign = binding["depth"]
    ras[axis] = center[axis] + sign * depth_mm

    check_in_field_ras(ras, frame)

    # RAS to Cube: the project origin is the field's lower corner.
    return tuple(ras[axis] - (center[axis] - half[axis]) for axis in RAS_AXES)


def cube_to_ras(cube, frame: Frame):
    """Return the RAS point (mm) for a Cube-frame (x, y, z).

    Cube counts from the field's lower corner; RAS counts from the field
    centre. This is the inverse of the shift at the end of convert().
    """
    center = frame.field_center_ras
    return tuple(
        cube[i] + center[axis] - frame.field_size_mm[axis] / 2.0
        for i, axis in enumerate(RAS_AXES)
    )


def ras_to_cube(ras, frame: Frame):
    """Return the Cube-frame (x, y, z) for an RAS point (mm).

    Cube counts from the field's lower corner; RAS counts from the field
    centre. This is the inverse of cube_to_ras().
    """
    center = frame.field_center_ras
    return tuple(
        ras[i] - center[axis] + frame.field_size_mm[axis] / 2.0
        for i, axis in enumerate(RAS_AXES)
    )