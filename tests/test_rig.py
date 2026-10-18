"""Tests for the rig → RAS transform.

Written down rather than assumed because the real rig will have to be registered
into the patient frame, and a rig silently left at the origin would put every
measured point in the wrong place while looking perfectly healthy.
"""

import json
import pathlib
import sys
from math import sqrt

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.rig import Rig, load_rig  # noqa: E402


def test_an_untouched_rig_leaves_a_point_where_it_is():
    assert Rig().to_ras((1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0)


def test_a_translation_is_added():
    rig = Rig(translation_mm=(10.0, -5.0, 2.0))
    assert rig.to_ras((1.0, 2.0, 3.0)) == (11.0, -3.0, 5.0)


def test_a_quarter_turn_about_the_third_axis_moves_R_onto_A():
    rig = Rig(rotation_deg=(0.0, 0.0, 90.0))
    r, a, s = rig.to_ras((1.0, 0.0, 0.0))
    assert r == pytest.approx(0.0, abs=1e-12)
    assert a == pytest.approx(1.0, abs=1e-12)
    assert s == pytest.approx(0.0, abs=1e-12)


def test_rotation_happens_before_translation():
    # rotate (1,0,0) a quarter turn onto +A, then shift it by 10 in R
    rig = Rig(translation_mm=(10.0, 0.0, 0.0), rotation_deg=(0.0, 0.0, 90.0))
    assert rig.to_ras((1.0, 0.0, 0.0)) == pytest.approx((10.0, 1.0, 0.0), abs=1e-12)


def test_the_transform_is_a_rotation_and_not_a_reflection():
    rig = Rig(rotation_deg=(20.0, -35.0, 47.0))
    x = rig.to_ras((1.0, 0.0, 0.0))
    y = rig.to_ras((0.0, 1.0, 0.0))
    z = rig.to_ras((0.0, 0.0, 1.0))
    assert sqrt(sum(c * c for c in x)) == pytest.approx(1.0, abs=1e-12)
    assert sqrt(sum(c * c for c in y)) == pytest.approx(1.0, abs=1e-12)
    assert sqrt(sum(c * c for c in z)) == pytest.approx(1.0, abs=1e-12)


def test_a_point_survives_the_round_trip():
    rig = Rig(translation_mm=(12.0, -3.0, 44.0), rotation_deg=(15.0, 25.0, -60.0))
    there = rig.to_ras((7.0, -11.0, 3.0))
    back = rig.to_rig(there)
    assert back == pytest.approx((7.0, -11.0, 3.0), abs=1e-9)


def test_loading_reads_the_file(tmp_path):
    path = tmp_path / "rig.json"
    path.write_text(json.dumps({"translation_mm": [1, 2, 3], "rotation_deg": [0, 0, 90]}))

    rig = load_rig(path)

    assert rig.translation_mm == (1.0, 2.0, 3.0)
    assert rig.rotation_deg == (0.0, 0.0, 90.0)


def test_a_missing_file_is_refused_by_name(tmp_path):
    missing = tmp_path / "rig.json"
    with pytest.raises(RuntimeError, match="rig.json"):
        load_rig(missing)


def test_malformed_json_is_refused_by_name(tmp_path):
    path = tmp_path / "rig.json"
    path.write_text("{not json")
    with pytest.raises(RuntimeError, match="rig.json"):
        load_rig(path)


def test_a_missing_key_is_refused(tmp_path):
    path = tmp_path / "rig.json"
    path.write_text(json.dumps({"translation_mm": [1, 2, 3]}))
    with pytest.raises(RuntimeError, match="rotation_deg"):
        load_rig(path)


def test_the_wrong_number_of_values_is_refused(tmp_path):
    path = tmp_path / "rig.json"
    path.write_text(json.dumps({"translation_mm": [1, 2], "rotation_deg": [0, 0, 0]}))
    with pytest.raises(RuntimeError, match="three"):
        load_rig(path)
