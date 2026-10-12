"""Tests for fitting the operating field to its view.

The operating field is anchored to the centre of its view, and its scale is
set so the whole field fits.

Two facts about Slicer's layout drive the design:

  * Slicer slice views are NOT square (for example 298 x 137 px in the
    default four-up layout).
  * A 100 mm cube therefore cannot fill such a view. Fitting the short side
    covers the field exactly on that axis, and the long axis overflows.

Overflow on the long axis is not a bug. Touching near the left edge of a wide
view really is outside a cube-shaped workspace, and raising OutOfFieldError
there is the correct and useful behaviour.

Centring is what makes this sound. Pixel 0 is a view corner, which is an
arbitrary distance from the field. The view centre, by contrast, is exactly the
slice origin, so it maps to the field centre without any assumption.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.converter import OutOfFieldError, convert  # noqa: E402
from targeting.field_fit import (  # noqa: E402
    ViewportTooSmall,
    fits_field,
    mm_per_pixel_for_fit,
    view_pixels_for_field,
)
from targeting.frame import Frame  # noqa: E402

FIELD_MM = 100.0
RED_WIDTH_PX = 298
RED_HEIGHT_PX = 137


def test_scale_comes_from_the_short_side_so_the_field_always_fits():
    # 100 mm must fit inside 137 px, so 0.7299 mm/px, not 100/298.
    assert mm_per_pixel_for_fit(FIELD_MM, (RED_WIDTH_PX, RED_HEIGHT_PX)) == pytest.approx(
        FIELD_MM / RED_HEIGHT_PX
    )


def test_view_pixels_for_field_is_the_inverse_of_the_scale():
    assert view_pixels_for_field(FIELD_MM, mm_per_pixel=0.5) == pytest.approx(200.0)


def test_a_view_too_small_to_show_the_field_is_rejected():
    with pytest.raises(ViewportTooSmall, match="Zoom out"):
        fits_field(field_size_mm=FIELD_MM, viewport_px=(20, 20))


def test_the_real_red_view_size_is_accepted():
    assert fits_field(FIELD_MM, (RED_WIDTH_PX, RED_HEIGHT_PX)) is True


def test_touching_the_view_centre_gives_the_field_centre():
    frame = _red_frame()
    assert convert("XY", 149.0, 68.5, 0.0, frame) == pytest.approx(
        (50.0, 50.0, 50.0), abs=0.01
    )


def test_short_axis_edges_are_exactly_the_field_edges():
    frame = _red_frame()
    scale = mm_per_pixel_for_fit(FIELD_MM, (RED_WIDTH_PX, RED_HEIGHT_PX))
    _x, y, _z = convert("XY", 149.0, 0.0, 0.0, frame)
    assert y == pytest.approx(0.0, abs=0.01)
    _x, y_high, _z = convert("XY", 149.0, RED_HEIGHT_PX, 0.0, frame)
    assert y_high == pytest.approx(100.0, abs=0.01)
    assert scale == pytest.approx(100.0 / 137)


def test_touching_past_the_short_axis_is_outside_the_field():
    frame = _red_frame()
    with pytest.raises(OutOfFieldError):
        convert("XY", 149.0, 138.0, 0.0, frame)


def test_long_axis_overflow_is_reported_as_outside_the_field():
    """The wide Red view extends past a cube field; that is real."""
    frame = _red_frame()
    with pytest.raises(OutOfFieldError, match="R="):
        convert("XY", 0.0, 68.5, 0.0, frame)


def test_depth_is_unaffected_by_view_geometry():
    frame = _red_frame()
    low = convert("XY", 149.0, 68.5, -50.0, frame)
    high = convert("XY", 149.0, 68.5, 50.0, frame)
    assert low[2] == pytest.approx(0.0, abs=0.01)
    assert high[2] == pytest.approx(100.0, abs=0.01)


def test_every_measured_view_shares_the_scale_when_views_match():
    """Slicer scales all three slice views alike, so one scale is correct."""
    scale = mm_per_pixel_for_fit(FIELD_MM, (RED_WIDTH_PX, RED_HEIGHT_PX))
    assert scale == mm_per_pixel_for_fit(FIELD_MM, (299, RED_HEIGHT_PX))


def _red_frame():
    scale = mm_per_pixel_for_fit(FIELD_MM, (RED_WIDTH_PX, RED_HEIGHT_PX))
    return Frame(
        field_size_mm={"R": FIELD_MM, "A": FIELD_MM, "S": FIELD_MM},
        field_center_ras={"R": 0.0, "A": 0.0, "S": 0.0},
        pixel_size_mm={"XY": scale, "XZ": scale, "YZ": scale},
        view_center_px={
            "XY": (RED_WIDTH_PX / 2.0, RED_HEIGHT_PX / 2.0),
            "XZ": (RED_WIDTH_PX / 2.0, RED_HEIGHT_PX / 2.0),
            "YZ": (RED_WIDTH_PX / 2.0, RED_HEIGHT_PX / 2.0),
        },
        view_axis_map={
            "XY": {"u": ("R", -1), "v": ("A", 1), "depth": ("S", 1)},
            "XZ": {"u": ("R", -1), "v": ("S", 1), "depth": ("A", 1)},
            "YZ": {"u": ("A", -1), "v": ("S", 1), "depth": ("R", 1)},
        },
    )