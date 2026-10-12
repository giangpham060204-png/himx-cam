"""Tests for the monitor-frame converter.

These tests use synthetic frame constants. They prove the arithmetic, not
the measured constants: those get filled into frame.json after running
tools/measure_now.py from Slicer's Python Console.

Pixel coordinates are measured from the view CENTRE, because Slicer centres a
slice view on the slice origin. See targeting/field_fit.py for why.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.converter import (  # noqa: E402
    OutOfFieldError,
    UnknownViewError,
    convert,
    cube_to_ras,
)
from targeting.frame import Frame, make_test_frame  # noqa: E402

FIELD_MM = 100.0


def test_touching_the_view_centre_at_zero_depth_gives_the_field_centre():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    assert convert("XY", 0.0, 0.0, 0.0, frame) == pytest.approx((50.0, 50.0, 50.0))


def test_touch_left_of_centre_gives_a_lower_r_when_the_sign_is_negative():
    # Real Slicer Red view: screen-right is -R, so moving right lowers R.
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    frame.view_axis_map["XY"]["u"] = ("R", -1)
    assert convert("XY", 10.0, 0.0, 0.0, frame)[0] == pytest.approx(40.0)


def test_touch_above_centre_raises_a_because_screen_down_is_minus_a():
    # Red (axial) view: screen-up is +A (nose-up radiological), so
    # screen-down is -A.
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    frame.view_axis_map["XY"]["v"] = ("A", -1)
    assert convert("XY", 0.0, -10.0, 0.0, frame)[1] == pytest.approx(60.0)
    assert convert("XY", 0.0, 10.0, 0.0, frame)[1] == pytest.approx(40.0)


def test_depth_is_the_third_coordinate_for_xy_view():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    assert convert("XY", 0.0, 0.0, -50.0, frame)[2] == pytest.approx(0.0)
    assert convert("XY", 0.0, 0.0, 50.0, frame)[2] == pytest.approx(100.0)


def test_view_centre_touch_resolves_to_the_same_point_in_every_view():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    for view in ("XY", "YZ", "XZ"):
        assert convert(view, 0.0, 0.0, 0.0, frame) == pytest.approx((50.0, 50.0, 50.0))


def test_each_view_shows_a_different_pair_of_axes():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    # Raising depth by 10 moves a different Cube axis in each view:
    # XY cannot show S, YZ cannot show R, XZ cannot show A.
    assert convert("XY", 0.0, 0.0, 10.0, frame) == pytest.approx((50.0, 50.0, 60.0))
    assert convert("YZ", 0.0, 0.0, 10.0, frame) == pytest.approx((60.0, 50.0, 50.0))
    assert convert("XZ", 0.0, 0.0, 10.0, frame) == pytest.approx((50.0, 60.0, 50.0))


def test_depth_supplies_the_axis_the_view_cannot_show():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    moved = convert("XY", 0.0, 0.0, 45.0, frame)
    base = convert("XY", 0.0, 0.0, 40.0, frame)
    assert moved[0] == pytest.approx(base[0])
    assert moved[1] == pytest.approx(base[1])
    assert moved[2] == pytest.approx(base[2] + 5.0)


def test_the_sign_is_read_from_the_frame_not_hardcoded():
    """Flipping the stored sign must mirror the result about the field centre."""
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    positive = convert("XY", 20.0, 0.0, 0.0, frame)[0]
    frame.view_axis_map["XY"]["u"] = ("R", -1)
    mirrored = convert("XY", 20.0, 0.0, 0.0, frame)[0]
    assert positive == pytest.approx(70.0)
    assert mirrored == pytest.approx(30.0)
    assert positive + mirrored == pytest.approx(100.0)


def test_a_touch_off_the_field_is_rejected_rather_than_guessed():
    """An out-of-range touch must raise, never return a clamped plausible value."""
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    with pytest.raises(OutOfFieldError, match="R="):
        convert("XY", 60.0, 0.0, 0.0, frame)


def test_all_three_views_map_each_of_ras_exactly_once():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    for view, binding in frame.view_axis_map.items():
        axes = [axis for axis, _sign in binding.values()]
        assert sorted(axes) == ["A", "R", "S"], f"{view} maps to {axes}"


def test_pixels_are_scaled_by_millimetres_per_pixel():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=0.5)
    assert convert("XY", 20.0, 0.0, 0.0, frame)[0] == pytest.approx(60.0)


def test_all_eight_field_corners_are_reachable():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    for u in (-50.0, 50.0):
        for v in (-50.0, 50.0):
            for depth in (-50.0, 50.0):
                x, y, z = convert("XY", u, v, depth, frame)
                assert x in (0.0, 100.0)
                assert y in (0.0, 100.0)
                assert z in (0.0, 100.0)


def test_touch_beyond_the_field_is_rejected():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    with pytest.raises(OutOfFieldError):
        convert("XY", 0.0, 60.0, 0.0, frame)


def test_depth_outside_the_field_is_rejected():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    with pytest.raises(OutOfFieldError, match="S="):
        convert("XY", 0.0, 0.0, 80.0, frame)


def test_unknown_view_name_is_rejected():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    with pytest.raises(UnknownViewError):
        convert("ZZ", 0.0, 0.0, 0.0, frame)


def test_a_frame_from_json_alone_cannot_convert_until_viewport_is_resolved():
    """frame.json no longer stores pixel geometry; say that clearly."""
    from targeting.converter import MissingViewportError

    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    frame.pixel_size_mm = {}
    frame.view_center_px = {}
    with pytest.raises(MissingViewportError, match="live_frame"):
        convert("XY", 0.0, 0.0, 0.0, frame)


def test_field_centre_offset_does_not_change_the_cube_result():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    frame.field_center_ras = {"R": 10.0, "A": 0.0, "S": 0.0}
    assert convert("XY", 0.0, 0.0, 0.0, frame) == pytest.approx((50.0, 50.0, 50.0))


def test_package_contains_no_slicer_import():
    package = pathlib.Path(__file__).resolve().parents[1] / "targeting"
    for path in package.glob("*.py"):
        assert "import slicer" not in path.read_text(), f"{path.name} imports slicer"


def test_frame_loads_from_json_round_trip(tmp_path):
    frame = make_test_frame(FIELD_MM, pixel_size_mm=0.42)
    frame.view_center_px["XY"] = (149.0, 68.5)
    path = tmp_path / "frame.json"
    frame.save(path)

    loaded = Frame.load(path)

    assert loaded.field_size_mm == frame.field_size_mm
    assert loaded.pixel_size_mm["XY"] == pytest.approx(0.42)
    assert loaded.view_center_px["XY"] == (149.0, 68.5)
    assert loaded.view_axis_map["XY"]["depth"] == frame.view_axis_map["XY"]["depth"]


def test_cube_corner_maps_to_the_ras_corner():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    assert cube_to_ras((0.0, 0.0, 0.0), frame) == pytest.approx((-50.0, -50.0, -50.0))


def test_cube_centre_maps_to_the_ras_centre():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    assert cube_to_ras((50.0, 50.0, 50.0), frame) == pytest.approx((0.0, 0.0, 0.0))


def test_cube_to_ras_follows_a_shifted_field():
    frame = make_test_frame(FIELD_MM, pixel_size_mm=1.0)
    frame.field_center_ras = {"R": 10.0, "A": 0.0, "S": 0.0}
    assert cube_to_ras((50.0, 50.0, 50.0), frame) == pytest.approx((10.0, 0.0, 0.0))