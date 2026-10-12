"""Tests for the in-Slicer verification script's pure parts.

The script itself runs inside Slicer (hovering the mouse is the input), but
its comparison logic is plain arithmetic and is what decides PASS/FAIL. If it
cannot catch a wrong sign, the whole verification is theatre.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import check_live_mapping as check  # noqa: E402
from targeting.converter import OutOfFieldError  # noqa: E402
from targeting.frame import make_test_frame  # noqa: E402


def _frame():
    frame = make_test_frame(100.0, pixel_size_mm=1.0)
    frame.view_axis_map["XY"] = {"u": ("R", -1), "v": ("A", -1), "depth": ("S", 1)}
    frame.view_center_px["XY"] = (100.0, 50.0)
    return frame


def test_cube_from_ras_counts_from_the_field_corner():
    frame = _frame()
    assert check.cube_from_ras((-50.0, -50.0, -50.0), frame) == (0.0, 0.0, 0.0)
    assert check.cube_from_ras((0.0, 0.0, 0.0), frame) == (50.0, 50.0, 50.0)


def test_a_consistent_hover_gives_zero_error():
    # 10px right of centre lowers R by 10; 20px above centre raises A by 20.
    sample = {
        "view": "XY",
        "u_px": 110.0,
        "v_px": 30.0,
        "depth_mm": 0.0,
        "slicer_ras": [-10.0, 20.0, 0.0],
    }
    errors, worst = check.compare_sample(sample, _frame())
    assert errors == pytest.approx([0.0, 0.0, 0.0])
    assert worst == pytest.approx(0.0)
    assert check.verdict(worst) == "PASS"


def test_an_inverted_vertical_sign_fails_loudly():
    frame = _frame()
    frame.view_axis_map["XY"]["v"] = ("A", 1)  # the old, wrong frame.json value
    sample = {
        "view": "XY",
        "u_px": 110.0,
        "v_px": 30.0,
        "depth_mm": 0.0,
        "slicer_ras": [-10.0, 20.0, 0.0],
    }
    _errors, worst = check.compare_sample(sample, frame)
    assert worst == pytest.approx(40.0)  # 20 - (-20)
    assert check.verdict(worst) == "FAIL"


def test_an_inverted_depth_sign_fails_loudly():
    frame = _frame()
    frame.view_axis_map["XY"]["depth"] = ("S", -1)
    sample = {
        "view": "XY",
        "u_px": 100.0,
        "v_px": 50.0,
        "depth_mm": 30.0,
        "slicer_ras": [0.0, 0.0, 30.0],
    }
    _errors, worst = check.compare_sample(sample, frame)
    assert worst == pytest.approx(60.0)
    assert check.verdict(worst) == "FAIL"


def test_verdict_edges_are_inclusive():
    assert check.verdict(check.PASS_TOLERANCE_MM) == "PASS"
    assert check.verdict(check.PASS_TOLERANCE_MM + 1e-9) == "FAIL"


def test_tolerance_is_about_one_display_pixel():
    """Real Slicer scale is 250mm per 137px; 2mm is just over one pixel."""
    pixel_mm = 250.0 / 137.0
    assert pixel_mm <= check.PASS_TOLERANCE_MM < 2.0 * pixel_mm


def test_a_hover_outside_the_field_raises_rather_than_guessing():
    sample = {
        "view": "XY",
        "u_px": 200.0,
        "v_px": 50.0,
        "depth_mm": 0.0,
        "slicer_ras": [-100.0, 0.0, 0.0],
    }
    with pytest.raises(OutOfFieldError):
        check.compare_sample(sample, _frame())


def test_guided_order_is_red_then_green_then_yellow():
    assert check.PHASE_ORDER == ("XY", "XZ", "YZ")


def test_slicer_pixel_flips_y_into_qt_space():
    # Qt rows count 0..H-1: row = H - 1 - y, hence the +1 inside the flip.
    assert check.slicer_pixel_from_xyz([10.0, 20.0], 137.0, 1.0) == (10.0, 116.0)
    assert check.slicer_pixel_from_xyz([10.0, 20.0], 137.0, 2.0) == (5.0, 126.5)


def test_the_script_reloads_project_modules_on_every_run():
    """A rerun in the same Slicer session must test the current code on
    disk: without reloading, sys.modules serves the previously imported
    module."""
    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / "tools" / "check_live_mapping.py").read_text()
    assert "def _refresh_project_imports()" in source
    assert "importlib.reload(frame)" in source
    assert "importlib.reload(converter)" in source
    assert "importlib.reload(view_mapping)" in source
    assert "importlib.reload(live_frame)" in source
    assert source.index("_refresh_project_imports()") < source.index(
        "def run():"
    ) or "run():\n    _refresh_project_imports()" in source


def test_the_script_never_derives_paths_from_dunder_file():
    """Console scripts must not derive paths from __file__.

    exec(open(...).read()) in Slicer's console leaves a wrong or missing
    __file__, so building the project path from it silently points elsewhere
    ("No module named 'targeting'"). The script must hardcode the root.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / "tools" / "check_live_mapping.py").read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "__file__" not in stripped, f"uses __file__: {stripped!r}"
    assert "PROJECT_ROOT = pathlib.Path(" in source
