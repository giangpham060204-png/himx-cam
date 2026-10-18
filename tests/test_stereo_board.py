"""Tests for the board geometry behind the stereo-sample check.

This harness re-derives, from OpenCV's own 13 stereo pairs, the calibration
and the 3D board that OpenCV published alongside them. It is the project's
only way to exercise calibration and triangulation against real images and
a published answer key without owning a camera, so the geometry it relies
on has to be pinned: a wrong board layout or a wrong notion of "one square
apart" would silently turn a good calibration into a reported failure, or
worse, a bad one into a pass.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

import stereo_board as chk  # noqa: E402


# --- board geometry -------------------------------------------------------


def test_object_points_are_a_planar_grid_of_the_given_spacing():
    points = chk.object_points(cols=9, rows=6, square=0.025)

    assert points.shape == (54, 3)
    assert np.allclose(points[:, 2], 0.0)
    assert np.allclose(points[0], [0.0, 0.0, 0.0])
    # columns advance along x, rows along y
    assert np.allclose(points[1], [0.025, 0.0, 0.0])
    assert np.allclose(points[9], [0.0, 0.025, 0.0])
    assert np.allclose(points[-1], [8 * 0.025, 5 * 0.025, 0.0])


def test_grid_side_lengths_counts_every_adjacent_pair_once():
    points = chk.object_points(cols=9, rows=6, square=0.025)

    sides = chk.grid_side_lengths(points, cols=9, rows=6)

    # (cols - 1) * rows horizontal neighbours + cols * (rows - 1) vertical
    assert sides.size == 8 * 6 + 9 * 5 == 93
    assert np.allclose(sides, 0.025)


def test_grid_side_lengths_notice_a_stretched_grid():
    points = chk.object_points(cols=9, rows=6, square=0.025).copy()
    points[:, 0] *= 2.0

    sides = chk.grid_side_lengths(points, cols=9, rows=6)

    assert sides.min() == pytest.approx(0.025)
    assert sides.max() == pytest.approx(0.050)


def test_object_points_reject_an_impossible_board():
    with pytest.raises(ValueError, match="at least 2"):
        chk.object_points(cols=1, rows=6, square=0.025)
    with pytest.raises(ValueError, match="square"):
        chk.object_points(cols=9, rows=6, square=0.0)


def test_grid_side_lengths_reject_a_point_count_that_is_not_the_board():
    points = chk.object_points(cols=9, rows=6, square=0.025)[:-1]

    with pytest.raises(ValueError, match="54"):
        chk.grid_side_lengths(points, cols=9, rows=6)


# --- planarity of the reconstructed board ---------------------------------


def test_fit_plane_recovers_an_axis_aligned_plane():
    rng = np.random.default_rng(0)
    points = rng.normal(size=(50, 3))
    points[:, 2] = 0.3

    centroid, normal = chk.fit_plane(points)

    assert np.allclose(centroid, points.mean(axis=0))
    assert np.linalg.norm(normal) == pytest.approx(1.0)
    assert abs(normal[2]) == pytest.approx(1.0)


def test_fit_plane_recovers_a_tilted_plane():
    rng = np.random.default_rng(1)
    tilt = np.array([1.0, 2.0, 3.0]) / np.sqrt(14.0)
    points = rng.normal(size=(50, 3))
    points -= np.outer(points @ tilt, tilt)  # flatten onto the plane through the origin

    centroid, normal = chk.fit_plane(points)

    assert abs(float(normal @ tilt)) == pytest.approx(1.0, abs=1e-9)
    # the centroid lies *in* the plane, which is not the same as being zero
    assert abs(float(centroid @ tilt)) == pytest.approx(0.0, abs=1e-9)


def test_fit_plane_refuses_too_few_points():
    with pytest.raises(ValueError, match="three"):
        chk.fit_plane(np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]))


def test_fit_plane_refuses_collinear_points():
    line = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0], [2.0, 2.0, 2.0], [3.0, 3.0, 3.0]])

    with pytest.raises(ValueError, match="collinear"):
        chk.fit_plane(line)


def test_planar_residuals_measure_distance_from_the_plane():
    plane = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    centroid, normal = chk.fit_plane(plane)

    assert np.allclose(chk.planar_residuals(plane, centroid, normal), 0.0)
    # unsigned: the sign of a fitted normal carries no meaning
    off_plane = np.array([[0.25, 0.25, 2.5], [0.25, 0.25, -0.5]])
    assert np.allclose(chk.planar_residuals(off_plane, centroid, normal), [2.5, 0.5])


# --- comparing our answer with the published one --------------------------
#
# The reference is OpenCV's own left_intrinsics.yml: fx = fy = 535.92,
# principal point (342.28, 235.57) for its 640x480 sample pairs.

REFERENCE = {"fx": 535.92, "fy": 535.92, "cx": 342.28, "cy": 235.57}


def test_compare_intrinsics_is_silent_when_the_numbers_agree():
    assert chk.compare_intrinsics(REFERENCE, REFERENCE) == []


def test_compare_intrinsics_names_the_value_that_drifted():
    complaints = chk.compare_intrinsics(REFERENCE, dict(REFERENCE, cx=352.28))

    assert [row["name"] for row in complaints] == ["cx"]
    assert complaints[0]["reference"] == pytest.approx(342.28)
    assert complaints[0]["measured"] == pytest.approx(352.28)
    assert complaints[0]["abs_error"] == pytest.approx(10.0)


def test_compare_intrinsics_only_complains_when_both_tolerances_are_broken():
    # a focal length is judged as a scale: 0.76 % out is quiet, 1.1 % is not
    assert chk.compare_intrinsics(REFERENCE, dict(REFERENCE, fx=540.0)) == []
    assert [r["name"] for r in chk.compare_intrinsics(REFERENCE, dict(REFERENCE, fx=541.92))] == ["fx"]
    # the principal point is judged in pixels: 4 px is fine, 6 px is not,
    # even though both are more than 1 % of 342
    assert chk.compare_intrinsics(REFERENCE, dict(REFERENCE, cx=346.28)) == []
    assert [r["name"] for r in chk.compare_intrinsics(REFERENCE, dict(REFERENCE, cx=348.28))] == ["cx"]


# --- fetching the sample --------------------------------------------------

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def test_download_sample_writes_every_file_it_was_asked_for(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    names = ["left01.jpg", "right01.jpg"]
    for name in names:
        (upstream / name).write_bytes(JPEG_BYTES)

    written = chk.download_sample(tmp_path / "cache", base_url=upstream.as_uri(), names=names)

    assert written == [tmp_path / "cache" / "left01.jpg", tmp_path / "cache" / "right01.jpg"]
    assert (tmp_path / "cache" / "left01.jpg").read_bytes() == JPEG_BYTES


def test_download_sample_reuses_files_that_are_already_cached(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "left01.jpg").write_bytes(JPEG_BYTES)

    # the source does not exist: reaching out to it would raise
    chk.download_sample(cache, base_url=(tmp_path / "gone").as_uri(), names=["left01.jpg"])


def test_download_sample_refuses_an_error_page_saved_as_an_image(tmp_path):
    # a missing sample comes back as the text "404: Not Found", which is not
    # a JPEG however much its file name says so
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "left10.jpg").write_bytes(b"404: Not Found")

    with pytest.raises(RuntimeError, match="left10.jpg"):
        chk.download_sample(tmp_path / "cache", base_url=upstream.as_uri(), names=["left10.jpg"])


# --- the projection matrix ------------------------------------------------


def test_projection_matrix_packs_rotation_and_translation_after_the_calibration():
    K = np.array([[800.0, 0.0, 320.0], [0.0, 800.0, 240.0], [0.0, 0.0, 1.0]])
    R = np.eye(3)
    t = np.array([0.1, -0.2, 0.3])

    P = chk.projection_matrix(K, R, t)

    assert P.shape == (3, 4)
    assert np.allclose(P[:, :3], K)
    assert np.allclose(P[:, 3], K @ t)
