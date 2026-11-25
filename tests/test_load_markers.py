"""Tests for the marker plate config and the pixel-to-plate map.

The config is the only place the plate's real geometry is written down, so a
wrong number there is a wrong millimetre everywhere downstream. These tests
cover the rules that can be checked without a camera: count, bounds, unique
labels, unique points, a real spread, and the homography itself.

The homography tests matter for a second reason. Four markers fit exactly, so
their residual is always zero -- a config error hides. Five markers make the
fit overdetermined and the residual able to say no.
"""

import json
import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from targeting import marker_plate as m  # noqa: E402

# A perspective map from plate millimetres to picture pixels. Deliberately not
# a similarity: a wrong model has to show up as an error, not hide in a scale.
PLATE_TO_PIXELS = np.array([[3.10, 0.42, 118.0],
                            [-0.28, 3.00, 71.0],
                            [0.00035, -0.00021, 1.0]])

FIVE = [("M1", (0.0, 0.0)), ("M2", (60.0, 0.0)), ("M3", (60.0, 40.0)),
        ("M4", (0.0, 40.0)), ("M5", (30.0, 20.0))]


def write_plate(tmp_path, markers, width=60.0, height=40.0,
                origin=(20.0, 30.0, 0.0)):
    payload = {
        "plate_mm": {"width": width, "height": height},
        "markers": [{"label": label, "mm": list(mm)} for label, mm in markers],
        "cube_origin_mm": list(origin),
    }
    path = tmp_path / "plate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def pixels_for(markers):
    """The picture pixels a known plate-to-pixel map gives those markers."""
    inverse = np.linalg.inv(PLATE_TO_PIXELS)
    rows = []
    for _label, (x, y) in markers:
        u, v, w = inverse @ np.array([x, y, 1.0])
        rows.append((u / w, v / w))
    return rows


def test_the_shipped_config_loads():
    plate = m.load_plate(ROOT / "marker-plate.json")
    assert plate["width"] > 0.0 and plate["height"] > 0.0
    assert len(plate["markers"]) == 5
    assert [marker["label"] for marker in plate["markers"]] == [
        "M1", "M2", "M3", "M4", "M5"]
    assert plate["coplanar"] is True


def test_z_defaults_to_zero(tmp_path):
    plate = m.load_plate(write_plate(tmp_path, FIVE))
    assert [marker["z"] for marker in plate["markers"]] == [0.0] * 5
    assert plate["z_spread"] == 0.0


def test_a_third_number_is_kept(tmp_path):
    markers = FIVE[:4] + [("M5", (30.0, 20.0, 0.6))]
    plate = m.load_plate(write_plate(tmp_path, markers))
    assert plate["markers"][4]["z"] == 0.6
    assert plate["coplanar"] is True
    assert plate["z_spread"] == pytest.approx(0.6)


def test_a_constant_z_is_still_one_plane(tmp_path):
    markers = [(label, (mm[0], mm[1], 4.0)) for label, mm in FIVE]
    plate = m.load_plate(write_plate(tmp_path, markers))
    assert plate["z_spread"] == 0.0
    assert plate["coplanar"] is True


def test_a_varying_z_refuses_the_pixel_map(tmp_path):
    markers = FIVE[:4] + [("M5", (30.0, 20.0, 6.5))]
    plate = m.load_plate(write_plate(tmp_path, markers))
    assert plate["coplanar"] is False
    note = m.coplanarity_note(plate)
    assert note is not None and "not coplanar" in note


def test_four_numbers_are_refused(tmp_path):
    markers = FIVE[:4] + [("M5", (30.0, 20.0, 1.0, 2.0))]
    with pytest.raises(RuntimeError, match="4 numbers"):
        m.load_plate(write_plate(tmp_path, markers))


def test_a_plate_needs_four_markers(tmp_path):
    path = write_plate(tmp_path, FIVE[:3])
    with pytest.raises(RuntimeError, match="at least four"):
        m.load_plate(path)


def test_a_marker_off_the_plate_is_refused(tmp_path):
    path = write_plate(tmp_path, FIVE + [("M6", (70.0, 10.0))])
    with pytest.raises(RuntimeError, match="outside the"):
        m.load_plate(path)


def test_a_repeated_label_is_refused(tmp_path):
    path = write_plate(tmp_path, FIVE[:4] + [("M1", (30.0, 20.0))])
    with pytest.raises(RuntimeError, match="repeated marker labels"):
        m.load_plate(path)


def test_two_markers_at_one_point_are_refused(tmp_path):
    path = write_plate(tmp_path, FIVE[:4] + [("M5", (60.0, 0.0))])
    with pytest.raises(RuntimeError, match="same point"):
        m.load_plate(path)


def test_collinear_markers_are_refused(tmp_path):
    """A line of markers fits perfectly and knows nothing about the second axis."""
    path = write_plate(tmp_path, [("M1", (0.0, 0.0)), ("M2", (20.0, 0.0)),
                                  ("M3", (40.0, 0.0)), ("M4", (60.0, 0.0)),
                                  ("M5", (30.0, 0.0))])
    with pytest.raises(RuntimeError, match="collinear"):
        m.load_plate(path)


def test_the_fit_recovers_a_known_map():
    plate_mm = [mm for _label, mm in FIVE]
    homography = m.fit_homography(pixels_for(FIVE), plate_mm)
    for (u, v), (x, y) in zip(pixels_for(FIVE), plate_mm):
        assert m.to_plate(homography, u, v) == pytest.approx((x, y), abs=1e-6)


def test_five_markers_expose_a_displaced_click():
    """The fifth marker is the witness: move it and the residual has to move."""
    plate_mm = [mm for _label, mm in FIVE]
    pixels = pixels_for(FIVE)
    clean = m.fit_homography(pixels, plate_mm)
    assert m.to_plate(clean, *pixels[4]) == pytest.approx(plate_mm[4], abs=1e-6)

    moved = list(pixels)
    moved[4] = (moved[4][0] + 6.0, moved[4][1] - 3.0)
    fitted = m.fit_homography(moved, plate_mm)
    error = [
        ((m.to_plate(fitted, u, v)[0] - x) ** 2
         + (m.to_plate(fitted, u, v)[1] - y) ** 2) ** 0.5
        for (u, v), (x, y) in zip(moved, plate_mm)
    ]
    assert max(error) > 1.0, f"a six-pixel move should be visible: {error}"


def test_four_markers_fit_exactly_and_cannot_say_no():
    """Why the default plate has five: with four, every config error is silent."""
    four = FIVE[:4]
    plate_mm = [mm for _label, mm in four]
    pixels = pixels_for(four)
    moved = list(pixels)
    moved[3] = (moved[3][0] + 40.0, moved[3][1] + 40.0)
    fitted = m.fit_homography(moved, plate_mm)
    # The map is wrong everywhere, yet the clicked points read back exactly.
    for (u, v), (x, y) in zip(moved, plate_mm):
        assert m.to_plate(fitted, u, v) == pytest.approx((x, y), abs=1e-6)
