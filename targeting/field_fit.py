"""Fit the operating field to its view.

The operating field is anchored to the centre of its view, and its scale is
set so the whole field fits.

Two facts about Slicer's layout drive this:

  * Slicer slice views are not square (for example 298 x 137 px in the
    default four-up layout).
  * A 100 mm cube therefore cannot fill one. Fitting the short side covers
    the field exactly on that axis, and the long axis overflows.

Overflow on the long axis is correct, not a defect: a touch near the left
edge of a wide view really is outside a cube-shaped workspace, and raising
OutOfFieldError there is the useful behaviour.

Centring is what makes this sound. Pixel 0 is a view corner, an arbitrary
distance from the field. The view centre is exactly the slice origin, so it
maps to the field centre with no assumption at all.
"""

from __future__ import annotations

TOLERANCE_MM = 1e-6


class ViewportTooSmall(ValueError):
    """Raised when a view cannot show the whole field at once."""


def short_side(viewport_px):
    """The smaller of a viewport's two pixel dimensions."""
    width_px, height_px = viewport_px
    if width_px <= 0 or height_px <= 0:
        raise ValueError(f"viewport_px must be positive, got {viewport_px}")
    return float(min(width_px, height_px))


def mm_per_pixel_for_fit(field_size_mm, viewport_px):
    """Millimetres per pixel when the field fits the view's short side."""
    if field_size_mm <= 0:
        raise ValueError(f"field_size_mm must be positive, got {field_size_mm}")
    return field_size_mm / short_side(viewport_px)


def view_pixels_for_field(field_size_mm, mm_per_pixel):
    """Pixels needed to show a field at a given scale."""
    if mm_per_pixel <= 0:
        raise ValueError(f"mm_per_pixel must be positive, got {mm_per_pixel}")
    return field_size_mm / mm_per_pixel


def fits_field(field_size_mm, viewport_px):
    """True when the whole field is visible in a viewport of this many pixels."""
    side = short_side(viewport_px)
    needed = view_pixels_for_field(field_size_mm, 1.0)
    if needed > side + TOLERANCE_MM:
        raise ViewportTooSmall(
            f"a {field_size_mm}mm field needs at least {needed:.0f} px on the short "
            f"side, but this view is only {side:.0f} px. Zoom out until the whole "
            "field fits."
        )
    return True


def view_center_px(viewport_px):
    """Pixel coordinates of a viewport's centre."""
    width_px, height_px = viewport_px
    return (width_px / 2.0, height_px / 2.0)
