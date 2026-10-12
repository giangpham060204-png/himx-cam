"""Tests for deriving view constants from Slicer's own view matrices.

The fixture matrices below are the exact `GetXYToRAS()` values of a default
Slicer 5.12.3 four-up layout (298x137 views; Green 299x137). The header of
vtkMRMLSliceNode documents that matrix as: "Matrix mapping from XY pixel
coordinates on an image window into RAS world coordinates". It already
contains the FOV, window size and orientation, and Slicer recomputes it
whenever any of those change, which makes it the single source for the
touch mapping.

The screen conventions under test:

    Red    (axial,   XY view): screen-right = -R, screen-up = +A
    Green  (coronal, XZ view): screen-right = -R, screen-up = +S
    Yellow (sagittal,YZ view): screen-right = -A, screen-up = +S

"screen-up = +A" for the axial view is the radiological nose-up convention.
The converter counts v DOWNWARD from the view centre (Qt convention), so the
v binding is the dominant direction of MINUS column 1.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.converter import MissingViewportError, OutOfFieldError, convert  # noqa: E402
from targeting.frame import Frame, make_test_frame  # noqa: E402
from targeting.view_mapping import (  # noqa: E402
    DegenerateView,
    NonIsotropicView,
    constants_for_view,
    frame_from_views,
)

S = 250.0 / 137.0  # 1.8248175182481752 mm/px; FOV_y / viewport height

RED = {  # axial: R right-left, A ant-post, S depth
    "width_px": 298.0,
    "height_px": 137.0,
    "slice_origin_ras": [0.0, 0.0, 0.0],
    "xy_to_ras": [
        [-S, 0.0, 0.0, S * 149.0],
        [0.0, S, 0.0, -S * 68.5],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ],
}

GREEN = {  # coronal: R right-left, S sup-inf, A depth
    "width_px": 299.0,
    "height_px": 137.0,
    "slice_origin_ras": [0.0, 0.0, 0.0],
    "xy_to_ras": [
        [-S, 0.0, 0.0, S * 149.5],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, S, 0.0, -S * 68.5],
        [0.0, 0.0, 0.0, 1.0],
    ],
}

YELLOW = {  # sagittal: A ant-post, S sup-inf, R depth
    "width_px": 298.0,
    "height_px": 137.0,
    "slice_origin_ras": [0.0, 0.0, 0.0],
    "xy_to_ras": [
        [0.0, 0.0, -1.0, 0.0],
        [-S, 0.0, 0.0, S * 149.0],
        [0.0, S, 0.0, -S * 68.5],
        [0.0, 0.0, 0.0, 1.0],
    ],
}

VIEW_DATA = {"XY": RED, "XZ": GREEN, "YZ": YELLOW}


def _base():
    return make_test_frame(100.0, pixel_size_mm=1.0)


def test_axial_view_directions_match_slicer():
    c = constants_for_view(RED)
    assert c.axis_map["u"] == ("R", -1), "radiological: patient left on screen right"
    assert c.axis_map["v"] == ("A", -1), "nose up: screen-down lowers A"
    assert c.axis_map["depth"] == ("S", 1)


def test_coronal_view_directions_match_slicer():
    c = constants_for_view(GREEN)
    assert c.axis_map["u"] == ("R", -1)
    assert c.axis_map["v"] == ("S", -1)
    assert c.axis_map["depth"] == ("A", 1), "looks from anterior: +A moves into screen"


def test_sagittal_view_directions_match_slicer():
    c = constants_for_view(YELLOW)
    assert c.axis_map["u"] == ("A", -1), "nose points screen-left"
    assert c.axis_map["v"] == ("S", -1)
    assert c.axis_map["depth"] == ("R", -1), "patient right is the near side"


def test_mm_per_pixel_is_the_matrix_scale_not_a_fit_default():
    """100/137 was a guess; Slicer's real scale is 250/137 for this layout."""
    c = constants_for_view(RED)
    assert c.mm_per_px == pytest.approx(250.0 / 137.0, abs=1e-9)
    assert c.mm_per_px != pytest.approx(100.0 / 137.0)


def test_the_view_middle_is_where_the_slice_origin_projects():
    for data in (RED, GREEN, YELLOW):
        c = constants_for_view(data)
        # Qt rows count 0..H-1, so the flip carries a -1 (SetEventPositionFlipY).
        assert c.center_px == pytest.approx(
            (data["width_px"] / 2.0, data["height_px"] / 2.0 - 1.0)
        )


def test_a_flipped_vertical_column_would_be_caught():
    """A flipped vertical column must be caught: v would invert to (A, +1)."""
    flipped = dict(RED)
    flipped = {
        **RED,
        "xy_to_ras": [
            [-S, 0.0, 0.0, S * 149.0],
            [0.0, -S, 0.0, -S * 68.5],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
    }
    assert constants_for_view(flipped).axis_map["v"] == ("A", 1)


def test_non_square_pixels_are_refused():
    bad = {
        **RED,
        "xy_to_ras": [
            [-S, 0.0, 0.0, 0.0],
            [0.0, S / 2.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
    }
    with pytest.raises(NonIsotropicView):
        constants_for_view(bad)


def test_a_degenerate_plane_is_refused():
    bad = {
        **RED,
        "xy_to_ras": [
            [0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
    }
    with pytest.raises(DegenerateView):
        constants_for_view(bad)


def test_an_empty_viewport_is_refused():
    with pytest.raises(ValueError):
        constants_for_view({**RED, "width_px": 0.0})


def test_frame_from_views_fills_all_three_views():
    frame = frame_from_views(_base(), VIEW_DATA)
    assert set(frame.pixel_size_mm) == {"XY", "XZ", "YZ"}
    assert set(frame.view_center_px) == {"XY", "XZ", "YZ"}
    assert frame.view_axis_map["YZ"]["depth"] == ("R", -1)


def test_derived_frame_converts_a_right_and_down_touch_the_slicer_way():
    frame = frame_from_views(_base(), VIEW_DATA)
    cu, cv = frame.view_center_px["XY"]

    right = convert("XY", cu + 10.0, cv, 0.0, frame)
    assert right[0] == pytest.approx(50.0 - 10.0 * S, abs=1e-9), "right lowers R"
    assert right[1] == pytest.approx(50.0, abs=1e-9)

    down = convert("XY", cu, cv + 10.0, 0.0, frame)
    assert down[1] == pytest.approx(50.0 - 10.0 * S, abs=1e-9), "down lowers A"


def test_derived_coronal_frame_maps_right_to_minus_r_and_down_to_minus_s():
    frame = frame_from_views(_base(), VIEW_DATA)
    cu, cv = frame.view_center_px["XZ"]
    right = convert("XZ", cu + 10.0, cv, 0.0, frame)
    down = convert("XZ", cu, cv + 10.0, 0.0, frame)
    assert right[0] == pytest.approx(50.0 - 10.0 * S, abs=1e-9)
    assert down[2] == pytest.approx(50.0 - 10.0 * S, abs=1e-9)


def test_derived_sagittal_frame_depth_uses_minus_r():
    frame = frame_from_views(_base(), VIEW_DATA)
    cu, cv = frame.view_center_px["YZ"]
    ahead = convert("YZ", cu, cv, 27.0, frame)
    assert ahead[0] == pytest.approx(50.0 - 27.0, abs=1e-9), "+depth goes toward R-"


def test_a_touch_outside_the_field_still_raises():
    frame = frame_from_views(_base(), VIEW_DATA)
    cu, cv = frame.view_center_px["XY"]
    with pytest.raises(OutOfFieldError):
        convert("XY", cu + 60.0, cv, 0.0, frame)


def test_a_view_missing_from_view_data_is_refused_at_convert_time():
    frame = frame_from_views(_base(), {"XY": RED})
    assert "XZ" not in frame.pixel_size_mm
    with pytest.raises(MissingViewportError):
        convert("XZ", 0.0, 0.0, 0.0, frame)


def test_a_display_scale_factor_rescales_pixels_to_qt_space():
    """Matrix pixels are device pixels; Qt reports logical pixels (HiDPI)."""
    data = {
        **RED,
        "width_px": 596.0,
        "height_px": 274.0,
        "qt_width_px": 298.0,
        "qt_height_px": 137.0,
        "xy_to_ras": [
            [-S / 2.0, 0.0, 0.0, S * 149.0],
            [0.0, S / 2.0, 0.0, -S * 68.5],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
    }
    c = constants_for_view(data)
    assert c.mm_per_px == pytest.approx(250.0 / 137.0, abs=1e-9), "per QT pixel"
    assert c.center_px == pytest.approx((149.0, 68.0))


def test_mismatched_display_scales_are_refused():
    data = {
        **RED,
        "width_px": 596.0,
        "height_px": 274.0,
        "qt_width_px": 298.0,
        "qt_height_px": 100.0,  # would mean different scale on each axis
    }
    with pytest.raises(NonIsotropicView):
        constants_for_view(data)


def test_stored_frame_json_matches_slicer_orientations():
    """frame.json is documentation; it must never contradict Slicer's conventions."""
    import json

    path = pathlib.Path(__file__).resolve().parents[1] / "frame.json"
    data = json.loads(path.read_text())

    assert "pixel_size_mm" not in data, "screen-dependent values must not be stored"
    assert "view_center_px" not in data, "screen-dependent values must not be stored"

    expected = {
        "XY": {"u": ["R", -1], "v": ["A", -1], "depth": ["S", 1]},
        "XZ": {"u": ["R", -1], "v": ["S", -1], "depth": ["A", 1]},
        "YZ": {"u": ["A", -1], "v": ["S", -1], "depth": ["R", -1]},
    }
    for view, binding in expected.items():
        stored = data["view_axis_map"][view]
        for name, (axis, sign) in binding.items():
            assert stored[name][0] == axis, f"{view}.{name} axis changed"
            assert stored[name][1] == sign, f"{view}.{name} sign changed"
