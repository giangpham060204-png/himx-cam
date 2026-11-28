"""Tests for the rigid virtual-to-real marker registration.

The registration is the only place a picked virtual point becomes a real
coordinate, so the tests cover the rules that can be checked without Slicer:
three non-collinear markers in both frames, unique labels, the rigid fit, the
reflection guard, the residual, and the saved file.
"""

import json
import math
import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from targeting import registration as r  # noqa: E402

THREE = [
    {"label": "B1", "virtual_mm": [0.0, 0.0, 0.0]},
    {"label": "B2", "virtual_mm": [60.0, 0.0, 0.0]},
    {"label": "T1", "virtual_mm": [30.0, 20.0, 0.0]},
]
FIVE = THREE + [
    {"label": "B3", "virtual_mm": [0.0, 40.0, 0.0]},
    {"label": "T2", "virtual_mm": [28.0, 22.0, 0.0]},
]


def registration(markers, frame="arm-base"):
    return {
        "real_frame": frame,
        "markers": [
            {"label": marker["label"],
             "virtual_mm": list(marker["virtual_mm"]),
             "real_mm": list(marker.get("real_mm", marker["virtual_mm"]))}
            for marker in markers
        ],
    }


def rot_z(degrees):
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def rot_x(degrees):
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


def moved(markers, rotation, translation):
    return [
        {**marker,
         "real_mm": list(rotation @ np.array(marker["virtual_mm"]) + translation)}
        for marker in markers
    ]


# -- the config ---------------------------------------------------------


def test_the_shipped_template_is_a_fittable_default():
    # The default local frame has three markers, one raised, and R = V, so
    # the panel can fit at startup. It is not a measured arm frame.
    loaded = r.load_registration(ROOT / "marker-registration.json")
    assert loaded["fittable"] is True
    assert len(loaded["markers"]) == 3
    fit = r.fit_registration(loaded)
    assert fit["rms"] == pytest.approx(0.0, abs=1e-9)
    # Three points always lie on a plane; the raised T1 makes the triangle
    # non-horizontal but cannot remove the mirror ambiguity by itself.
    assert fit["coplanar"] is True


def test_three_non_collinear_markers_are_accepted():
    value = r.validate_registration(registration(THREE), "test")
    assert value["real_frame"] == "arm-base"
    assert [marker["label"] for marker in value["markers"]] == [
        "B1", "B2", "T1"]


def test_two_markers_are_refused():
    with pytest.raises(RuntimeError, match="at least 3"):
        r.validate_registration(registration(THREE[:2]), "test")


def test_collinear_real_markers_are_not_fittable():
    markers = [{"label": "A", "virtual_mm": [0, 0, 0],
                "real_mm": [0, 0, 0]},
               {"label": "B", "virtual_mm": [60, 0, 0],
                "real_mm": [30, 0, 0]},
               {"label": "C", "virtual_mm": [0, 40, 0],
                "real_mm": [60, 0, 0]}]
    value = r.validate_registration(registration(markers), "test")
    assert value["fittable"] is False
    assert "real markers are collinear" in value["note"]
    with pytest.raises(ValueError, match="real markers are collinear"):
        r.fit_registration(value)


def test_collinear_virtual_markers_are_not_fittable():
    markers = [{"label": "A", "virtual_mm": [0, 0, 0],
                "real_mm": [0, 0, 0]},
               {"label": "B", "virtual_mm": [30, 0, 0],
                "real_mm": [30, 0, 0]},
               {"label": "C", "virtual_mm": [60, 0, 0],
                "real_mm": [0, 40, 0]}]
    value = r.validate_registration(registration(markers), "test")
    assert value["fittable"] is False
    assert "virtual markers are collinear" in value["note"]
    with pytest.raises(ValueError, match="virtual markers are collinear"):
        r.fit_registration(value)


def test_repeated_labels_are_refused():
    markers = [{"label": "A", "virtual_mm": [0, 0, 0],
                "real_mm": [0, 0, 0]},
               {"label": "A", "virtual_mm": [60, 0, 0],
                "real_mm": [60, 0, 0]},
               {"label": "C", "virtual_mm": [0, 40, 0],
                "real_mm": [0, 40, 0]}]
    with pytest.raises(RuntimeError, match="repeated marker labels"):
        r.validate_registration(registration(markers), "test")


def test_real_mm_needs_three_numbers():
    markers = [{"label": "A", "virtual_mm": [0, 0, 0],
                "real_mm": [0, 0]},
               {"label": "B", "virtual_mm": [60, 0, 0],
                "real_mm": [60, 0, 0]},
               {"label": "C", "virtual_mm": [0, 40, 0],
                "real_mm": [0, 40, 0]}]
    with pytest.raises(RuntimeError, match="three numbers"):
        r.validate_registration(registration(markers), "test")


def test_non_finite_real_mm_is_refused():
    markers = [{"label": "A", "virtual_mm": [0, 0, 0],
                "real_mm": [0, 0, float("nan")]},
               {"label": "B", "virtual_mm": [60, 0, 0],
                "real_mm": [60, 0, 0]},
               {"label": "C", "virtual_mm": [0, 40, 0],
                "real_mm": [0, 40, 0]}]
    with pytest.raises(RuntimeError, match="finite"):
        r.validate_registration(registration(markers), "test")


def test_real_frame_is_required():
    value = registration(THREE)
    value["real_frame"] = "  "
    with pytest.raises(RuntimeError, match="real_frame"):
        r.validate_registration(value, "test")


def test_save_and_load_keep_the_note(tmp_path):
    path = tmp_path / "registration.json"
    path.write_text(json.dumps({
        "note": "measured 2026-11-27",
        **registration(THREE),
    }), encoding="utf-8")
    loaded = r.load_registration(path)
    saved = r.save_registration(loaded, path)
    text = json.loads(saved.read_text(encoding="utf-8"))
    assert text["note"] == "measured 2026-11-27"
    assert text["real_frame"] == "arm-base"
    assert text["markers"][0]["virtual_mm"] == [0.0, 0.0, 0.0]
    assert text["markers"][0]["real_mm"] == [0.0, 0.0, 0.0]


# -- the fit ------------------------------------------------------------


def test_identity_fit():
    points = [tuple(marker["virtual_mm"]) for marker in THREE]
    fit = r.fit_transform(points, points)
    assert fit["rms"] == pytest.approx(0.0, abs=1e-9)
    assert fit["scale"] == pytest.approx(1.0)
    assert r.apply_transform(fit, (1.0, 2.0, 3.0)) == pytest.approx(
        (1.0, 2.0, 3.0))


def test_a_known_rigid_transform_is_recovered():
    rotation = rot_z(30.0) @ rot_x(20.0)
    translation = np.array([12.0, -7.0, 4.0])
    markers = moved(THREE, rotation, translation)
    fit = r.fit_registration(r.validate_registration(
        registration(markers), "test"))
    assert np.allclose(np.asarray(fit["rotation"]), rotation, atol=1e-9)
    assert np.allclose(np.asarray(fit["translation"]), translation, atol=1e-9)
    assert fit["rms"] == pytest.approx(0.0, abs=1e-9)
    probe = np.array([10.0, 5.0, 2.0])
    expected = rotation @ probe + translation
    assert r.apply_transform(fit, probe) == pytest.approx(tuple(expected))


def test_three_markers_are_enough():
    rotation = rot_z(-15.0)
    translation = np.array([3.0, 5.0, -2.0])
    markers = moved(THREE, rotation, translation)
    fit = r.fit_registration(r.validate_registration(
        registration(markers), "test"))
    assert fit["count"] == 3
    assert fit["rms"] == pytest.approx(0.0, abs=1e-9)


def test_noise_is_reported_as_a_residual():
    rng = np.random.default_rng(11)
    rotation = rot_z(10.0)
    translation = np.array([1.0, 2.0, 3.0])
    markers = moved(FIVE, rotation, translation)
    for marker in markers:
        marker["real_mm"] = list(
            np.array(marker["real_mm"]) + rng.normal(0.0, 0.5, 3))
    fit = r.fit_registration(r.validate_registration(
        registration(markers), "test"))
    assert fit["rms"] > 0.0
    assert fit["max"] >= fit["rms"]
    assert len(fit["residuals"]) == 5


def test_a_reflection_of_a_non_coplanar_set_is_not_fitted():
    markers = [
        {"label": "A", "virtual_mm": [0.0, 0.0, 0.0]},
        {"label": "B", "virtual_mm": [60.0, 0.0, 0.0]},
        {"label": "C", "virtual_mm": [0.0, 40.0, 0.0]},
        {"label": "D", "virtual_mm": [10.0, 10.0, 15.0]},
    ]
    for marker in markers:
        x, y, z = marker["virtual_mm"]
        marker["real_mm"] = [2.0 * 30.0 - x, y, z]
    fit = r.fit_registration(r.validate_registration(
        registration(markers), "test"))
    assert abs(np.linalg.det(np.asarray(fit["rotation"])) - 1.0) < 1e-9
    assert fit["rms"] > 1.0  # a proper rotation cannot reproduce a mirror
    assert fit["coplanar"] is False


def test_a_coplanar_mirror_is_a_known_ambiguity():
    # A reflection of a coplanar set is a proper rotation about an in-plane
    # axis (the normal flips). The points alone cannot tell the poses apart;
    # the fit must say so instead of pretending the residual caught it.
    markers = []
    for marker in THREE:
        x, y, z = marker["virtual_mm"]
        markers.append({**marker, "real_mm": [2.0 * 30.0 - x, y, z]})
    fit = r.fit_registration(r.validate_registration(
        registration(markers), "test"))
    assert fit["coplanar"] is True
    assert fit["rms"] == pytest.approx(0.0, abs=1e-9)
    assert "mirror flip" in r.fit_note(fit)


def test_collinear_virtual_points_are_refused():
    with pytest.raises(ValueError, match="virtual markers are collinear"):
        r.fit_transform([(0, 0, 0), (10, 0, 0), (20, 0, 0)],
                        [(0, 0, 0), (10, 0, 0), (20, 0, 0)])


def test_too_few_points_are_refused():
    with pytest.raises(ValueError, match="at least 3"):
        r.fit_transform([(0, 0, 0), (10, 0, 0)],
                        [(0, 0, 0), (10, 0, 0)])


def test_fit_payload_records_every_marker():
    rotation = rot_z(5.0)
    translation = np.array([1.0, 2.0, 3.0])
    markers = moved(THREE, rotation, translation)
    reg = r.validate_registration(registration(markers), "test")
    fit = r.fit_registration(reg)
    payload = r.fit_payload(fit, reg)
    assert payload["rms_mm"] == 0.0
    assert len(payload["markers"]) == 3
    assert payload["markers"][0]["label"] == "B1"
    assert payload["markers"][0]["error_mm"] == 0.0


def test_fit_note_warns_above_the_residual_limit():
    markers = moved(FIVE, rot_z(0.0), np.array([0.0, 0.0, 0.0]))
    markers[0]["real_mm"] = [5.0, 5.0, 5.0]
    fit = r.fit_registration(r.validate_registration(
        registration(markers), "test"))
    note = r.fit_note(fit)
    assert "WARNING" in note
