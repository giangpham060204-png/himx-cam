"""One clicked pixel as a 3D ray and its epipolar line on another picture.

This is the geometry of the guide, deliberately separate from Slicer:

* a click in camera A is a ray from A's centre through the scene;
* in the 3D view that ray can be drawn as a finite line;
* in camera B the ray projects to a line, the epipolar line;
* in A's own picture the same ray projects to the clicked point.

Nothing here invents a 3D point. The guide says where a matching point *could*
be, never where it *is*.

Pure standard library: this module also imports cleanly inside Slicer.
"""

from __future__ import annotations

import math

from targeting.triangulation import project, ray_for

DEFAULT_RAY_LENGTH_MM = 1000.0


def ray_segment(projection, pixel, length_mm=DEFAULT_RAY_LENGTH_MM):
    """A finite piece of one camera's line of sight, in the rig frame.

    Returns ``(origin, endpoint)``. The length is only for drawing; the ray
    itself is infinite and the guide does not claim a depth.
    """
    length_mm = float(length_mm)
    if not math.isfinite(length_mm) or length_mm <= 0.0:
        raise ValueError("a ray guide needs a positive finite length")
    origin, direction = ray_for(projection, (float(pixel[0]), float(pixel[1])))
    endpoint = tuple(
        float(origin[axis]) + float(direction[axis]) * length_mm
        for axis in range(3)
    )
    return tuple(float(value) for value in origin), endpoint


def epipolar_segment(projection_a, pixel_a, projection_b, width, height):
    """The click ray from camera A, clipped to camera B's picture.

    Returns two image points ``((u0, v0), (u1, v1))`` or ``None`` when the
    ray does not cross B's picture. The line is computed from two points on
    A's ray; it is a guide, not a depth estimate.
    """
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        return None

    origin, direction = ray_for(
        projection_a, (float(pixel_a[0]), float(pixel_a[1]))
    )
    projected = []
    # The line is independent of which two points we choose. A few distances
    # make this robust when one choice lands exactly on camera B's own plane.
    for length_mm in (0.0, DEFAULT_RAY_LENGTH_MM, 500.0, 2000.0):
        point = tuple(
            float(origin[axis]) + float(direction[axis]) * length_mm
            for axis in range(3)
        )
        try:
            image_point = project(projection_b, point)
        except ValueError:
            continue
        if not all(math.isfinite(value) for value in image_point):
            continue
        if all(
            math.hypot(image_point[0] - other[0],
                       image_point[1] - other[1]) > 1e-9
            for other in projected
        ):
            projected.append((float(image_point[0]), float(image_point[1])))
        if len(projected) == 2:
            break
    if len(projected) < 2:
        return None
    return clip_line(projected[0], projected[1], width, height)


def clip_line(first, second, width, height):
    """Clip the *infinite* line through two points to the image rectangle.

    An epipolar line is a line, not the finite segment between two samples on
    the ray: the sample points can both fall inside the picture, which used to
    make the drawn line stop before the matching click.
    """
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        return None

    x0, y0 = float(first[0]), float(first[1])
    x1, y1 = float(second[0]), float(second[1])
    dx = x1 - x0
    dy = y1 - y0
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
        return None
    if abs(dx) < 1e-15 and abs(dy) < 1e-15:
        return None

    xmin, ymin = 0.0, 0.0
    xmax, ymax = float(width - 1), float(height - 1)
    lower, upper = -math.inf, math.inf
    for edge, offset in (
        (-dx, x0 - xmin),
        (dx, xmax - x0),
        (-dy, y0 - ymin),
        (dy, ymax - y0),
    ):
        if abs(edge) < 1e-15:
            if offset < 0.0:
                return None
            continue
        ratio = offset / edge
        if edge < 0.0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None

    if not (math.isfinite(lower) and math.isfinite(upper)):
        return None
    return (
        (x0 + lower * dx, y0 + lower * dy),
        (x0 + upper * dx, y0 + upper * dy),
    )


def clip_segment(start, end, width, height):
    """Clip a 2D line to the image rectangle, or return None if it misses.

    Liang-Barsky with the rectangle ``[0, width - 1] x [0, height - 1]``.
    """
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        return None

    x0, y0 = float(start[0]), float(start[1])
    x1, y1 = float(end[0]), float(end[1])
    dx = x1 - x0
    dy = y1 - y0
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
        return None

    xmin, ymin = 0.0, 0.0
    xmax, ymax = float(width - 1), float(height - 1)
    lower = 0.0
    upper = 1.0
    for edge, offset in (
        (-dx, x0 - xmin),
        (dx, xmax - x0),
        (-dy, y0 - ymin),
        (dy, ymax - y0),
    ):
        if abs(edge) < 1e-15:
            if offset < 0.0:
                return None
            continue
        ratio = offset / edge
        if edge < 0.0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None

    return (
        (x0 + lower * dx, y0 + lower * dy),
        (x0 + upper * dx, y0 + upper * dy),
    )
