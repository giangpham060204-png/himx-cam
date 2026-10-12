"""Tests for the verification pass/fail logic.

The point of the verification is to catch a sign error. These tests confirm the
threshold is tight enough to catch one, and that a correct setup passes.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import verify_converter as v  # noqa: E402
from targeting.frame import make_test_frame  # noqa: E402

FIELD_MM = 100.0

REFERENCE_ROWS = [
    {"label": "P1_corner", "ras": [-50.0, -50.0, -50.0], "cube": [0.0, 0.0, 0.0]},
    {"label": "P2_R100", "ras": [50.0, -50.0, -50.0], "cube": [100.0, 0.0, 0.0]},
    {"label": "P5_centre", "ras": [0.0, 0.0, 0.0], "cube": [50.0, 50.0, 50.0]},
]


def _frame():
    return make_test_frame(FIELD_MM, pixel_size_mm=0.73)


def test_ras_to_cube_adds_half_the_field():
    assert v.cube_from_ras((-50.0, -50.0, -50.0), (50.0, 50.0, 50.0)) == (0.0, 0.0, 0.0)
    assert v.cube_from_ras((0.0, 0.0, 0.0), (50.0, 50.0, 50.0)) == (50.0, 50.0, 50.0)


def test_a_correct_setup_passes_with_zero_error():
    worst, results = v.check(REFERENCE_ROWS, _frame())
    assert worst == pytest.approx(0.0)
    assert all(r["error_mm"] == pytest.approx(0.0) for r in results)


def test_a_flipped_r_sign_fails_the_threshold():
    """The whole reason this verification exists."""
    rows = [dict(r) for r in REFERENCE_ROWS]
    rows[1]["cube"] = [0.0, 0.0, 0.0]  # simulate the R sign being wrong
    worst, _results = v.check(rows, _frame())
    assert worst >= v.PASS_MM


def test_threshold_is_far_below_one_pixel():
    """0.01mm must be far below a plausible real mistake.

    One pixel is 0.73mm at this scale, so the threshold sits about 70x below
    it. That gap is what makes the check meaningful: a rounding difference
    passes, a real mistake does not.
    """
    scale = 0.729927
    assert v.PASS_MM < scale / 10.0


def test_check_reports_every_reference_point():
    _worst, results = v.check(REFERENCE_ROWS, _frame())
    assert len(results) == len(REFERENCE_ROWS)
    assert {r["label"] for r in results} == {r["label"] for r in REFERENCE_ROWS}


def test_check_rejects_a_wrong_sign_as_an_error_not_a_guess():
    """A row whose depth is outside the field must surface as an error."""
    rows = [{"label": "far", "ras": [0.0, 0.0, 200.0], "cube": [50.0, 50.0, 250.0]}]
    _worst, results = v.check(rows, _frame())
    assert results[0]["converted_error"] is not None