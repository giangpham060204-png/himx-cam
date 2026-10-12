"""Tests for the reference-point verification helper.

The check exists to catch a sign error in the converter. A sign error shows up
as roughly one field width at a corner and is nearly invisible at the centre,
so the reference set must include corners. That is the property under test
here, alongside the pure Cube/RAS relationship used to build it.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import make_refpoints as m  # noqa: E402

FIELD_MM = 100.0


def test_reference_set_includes_all_eight_corners_or_the_extremes():
    """A sign error must produce a visible error, so extremes are required."""
    cubes = [cube for _label, _ras, cube in m.REFERENCE_POINTS]
    for axis in range(3):
        values = sorted(c[axis] for c in cubes)
        assert values[0] == 0.0, f"axis {axis} never reaches the low corner"
        assert values[-1] == FIELD_MM, f"axis {axis} never reaches the high corner"


def test_reference_points_cover_the_field_centre():
    assert (FIELD_MM / 2.0,) * 3 in [cube for _l, _r, cube in m.REFERENCE_POINTS]


def test_ras_values_are_cube_minus_half_the_field():
    """Cube counts from the corner, RAS from the centre, so RAS = Cube - 50."""
    for _label, ras, cube in m.REFERENCE_POINTS:
        assert ras == pytest.approx(tuple(c - FIELD_MM / 2.0 for c in cube))


def test_a_sign_error_would_be_visible_on_this_reference_set():
    """Simulate flipping the R sign and confirm at least one point moves a lot."""
    worst = 0.0
    for _label, ras, cube in m.REFERENCE_POINTS:
        flipped_r = FIELD_MM - cube[0]
        worst = max(worst, abs(flipped_r - cube[0]))
    assert worst >= FIELD_MM / 2.0


def test_report_prints_label_ras_and_expected_cube():
    class FakeNode:
        def __init__(self):
            self.points = [(label, ras) for label, ras, _c in m.REFERENCE_POINTS]

        def GetNumberOfControlPoints(self):
            return len(self.points)

        def GetNthControlPointLabel(self, i):
            return self.points[i][0]

        def GetNthControlPointPosition(self, i):
            return self.points[i][1]

    text, rows = m.report(FakeNode())

    assert "P1_corner" in text
    assert "-50.000" in text
    assert len(rows) == 5
    assert rows[0]["label"] == "P1_corner"
    assert rows[0]["cube"] == [0.0, 0.0, 0.0]


def test_every_reference_label_is_unique():
    labels = [label for label, _ras, _cube in m.REFERENCE_POINTS]
    assert len(labels) == len(set(labels))


def test_reference_cube_values_are_inside_the_field():
    for _label, _ras, cube in m.REFERENCE_POINTS:
        for value in cube:
            assert 0.0 <= value <= FIELD_MM