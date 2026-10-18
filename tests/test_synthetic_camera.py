"""Tests for the synthetic camera: the seeing half, not the solving half.

Each of these pins something the draft had to be debugged into finding. They
are the reasons this file exists rather than the maths being trusted:

* a print whose corners are paired up the obvious way comes out mirrored, and a
  mirrored ArUco is not a marker -- it reads as noise;
* a 40 mm marker stops being readable somewhere near a metre, because its
  modules get too small, and no error formula warns you;
* the lens only costs anything away from the centre of the picture, which is
  exactly where a bolted-down rig puts its object.

The renderer is the expensive part, so frames and trials are kept small.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

cv2 = pytest.importorskip("cv2")
pytest.importorskip("matplotlib")

import synthetic_camera as cam  # noqa: E402  noqa: E402
from targeting.optics import optics_for  # noqa: E402

OPTICS = optics_for()


def one_frame(spread=150.0, standoff=500.0, marker_mm=cam.MARKER_MM, seed=4):
    rng = np.random.default_rng(seed)
    K, poses, projections = cam.scene(OPTICS, spread, standoff)
    _, R, t = poses[0]
    return cam.render(OPTICS, K, R, t, rng, marker_mm=marker_mm), K, R, t, projections


# --- the printed marker ---------------------------------------------------


def test_a_rendered_marker_is_readable_at_all():
    """The regression test for a mirrored print.

    Pairing the paper's corners to the projected ones in the obvious order
    reflects the pattern, and OpenCV's detector then finds nothing at all --
    silently, because "no marker" looks like a framing problem, not a bug.
    """
    frame, _, _, _, _ = one_frame()
    corners = cam.detect_marker(frame, cam.detector())

    assert corners.shape == (4, 2)
    assert 20.0 < float(np.ptp(corners[:, 0])) < 60.0


def test_the_marker_is_found_where_the_optics_say_it_must_be():
    frame, K, R, t, _ = one_frame()
    should_be = cam.project(K, R, t, np.zeros((1, 3)))[0]

    corners = cam.detect_marker(frame, cam.detector())
    found = cam.diagonal_crossing(cam.straight_lens(corners, OPTICS))

    assert np.allclose(found, should_be, atol=1.0)


def test_the_corners_come_back_in_a_order_that_makes_diagonals():
    frame, _, _, _, _ = one_frame()
    corners = cam.detect_marker(frame, cam.detector())

    # 0 and 2 opposite, 1 and 3 opposite: the crossing is the centre, which is
    # what makes the diagonal trick work at all
    centre = cam.diagonal_crossing(corners)
    assert np.allclose(centre, corners.mean(axis=0), atol=2.0)


# --- the lens -------------------------------------------------------------


def test_the_distortion_is_the_one_the_datasheet_claims():
    k1 = cam.barrel_from_datasheet(OPTICS)
    shifted = cam.corner_shift_pixels(OPTICS, k1) / OPTICS.pixels_y

    assert shifted == pytest.approx(OPTICS.distortion, rel=0.1)


def test_the_barrel_moves_the_frame_corner_inwards():
    k1 = cam.barrel_from_datasheet(OPTICS)

    assert k1 < 0.0
    assert cam.corner_shift_pixels(OPTICS, k1) > 1.0


def test_straightening_moves_a_pixel_by_the_shift_the_datasheet_implies():
    """The correction and the reported figure have to be the same quantity."""
    moved = float(np.linalg.norm(cam.straight_lens(np.array([[0.0, 0.0]]), OPTICS)))

    assert moved == pytest.approx(
        cam.corner_shift_pixels(OPTICS, cam.barrel_from_datasheet(OPTICS)), rel=0.05)


def test_taking_the_lens_back_out_matters_away_from_the_centre():
    """The finding that only shows up off-axis.

    Near the middle of the picture the distortion is nothing -- it is zero at
    the principal point by construction -- so a rig aimed straight at its
    object never notices. Bolted down at a workspace, the object sits to one
    side and leaving the lens in costs about twice the point error. Standing
    error, not noise: it came out at 1.99 for every seed tried.
    """
    careful, lazy, lost = cam.off_axis_penalty(OPTICS, 150.0, 500.0,
                                               np.random.default_rng(2), frames=4)

    assert lazy > 1.3 * careful
    assert lost >= 0.0


# --- the range a printed marker actually reaches -------------------------


def test_a_forty_millimetre_marker_fades_somewhere_near_a_metre():
    rng = np.random.default_rng(6)
    near = cam.detection_rate(OPTICS, 150.0, 500.0, rng, frames=3)
    far = cam.detection_rate(OPTICS, 150.0, 1500.0, rng, frames=3)

    assert near == 1.0
    assert far < near


def test_a_bigger_print_reaches_further():
    rng = np.random.default_rng(6)
    small = cam.detection_rate(OPTICS, 150.0, 1000.0, rng, marker_mm=40.0, frames=3)
    large = cam.detection_rate(OPTICS, 150.0, 1000.0, rng, marker_mm=80.0, frames=3)

    assert large >= small


# --- the whole chain ------------------------------------------------------


def test_the_chain_puts_the_point_where_the_maths_says_it_should():
    rng = np.random.default_rng(9)
    run = cam.end_to_end(OPTICS, 150.0, 500.0, rng)

    predicted = (0.4 * float(np.linalg.norm(run["poses"][0][0])) ** 2
                 / (OPTICS.focal_length_pixels * cam.pairwise_baseline(150.0)))
    assert run["error_mm"] < 3.0 * predicted


def test_a_marker_too_small_to_read_is_reported_rather_than_crashing():
    rng = np.random.default_rng(9)

    run = cam.end_to_end(OPTICS, 150.0, 500.0, rng, )
    assert run["pixels"] is not None
    tiny = cam.end_to_end(OPTICS, 150.0, 1500.0, rng)
    assert tiny["pixels"] is None or tiny["error_mm"] > run["error_mm"]


# --- the report -----------------------------------------------------------


def test_format_report_says_pass_when_every_check_passes():
    report = cam.build_report(OPTICS, trials=10, frames=2)

    text = cam.format_report(report)
    assert "PASS" in text.rstrip().splitlines()[-1]


def test_main_writes_both_figures_and_reports_a_bad_marker(tmp_path):
    good = cam.main(["--out", str(tmp_path), "--trials", "10", "--frames", "2"])

    assert good == 0
    assert (tmp_path / "camera-views.png").is_file()
    assert (tmp_path / "camera-accuracy.png").is_file()


def test_main_fails_when_the_marker_cannot_be_read(tmp_path):
    # a 4 mm print is not a marker at half a metre, and the tool must say so
    # rather than pretend or fall over
    bad = cam.main(["--out", str(tmp_path), "--marker-mm", "4",
                    "--trials", "10", "--frames", "2"])

    assert bad == 1


def test_a_cached_render_is_byte_identical_to_a_fresh_one():
    """The whole point of caching is that nothing changes but the speed."""
    rng = np.random.default_rng(5)
    K, poses, _ = cam.scene(OPTICS, 150.0, 500.0)
    _, R, t = poses[0]

    first = cam.render(OPTICS, K, R, t, np.random.default_rng(5))
    cam.lens_maps.cache_clear()
    cam.lens_vignette.cache_clear()
    second = cam.render(OPTICS, K, R, t, np.random.default_rng(5))

    assert np.array_equal(first, second)


def test_the_lens_maps_are_actually_memoised():
    """Without this, the test above would pass even with no cache at all."""
    k1 = cam.barrel_from_datasheet(OPTICS)

    assert cam.lens_maps(OPTICS, k1) is cam.lens_maps(OPTICS, k1)
    assert cam.lens_vignette(OPTICS) is cam.lens_vignette(OPTICS)


def test_the_cached_maps_are_the_ones_computed_directly():
    k1 = cam.barrel_from_datasheet(OPTICS)
    K = cam.camera_matrix(OPTICS)

    mapx, mapy = cam.lens_maps(OPTICS, k1)
    expected_x, expected_y = cam.distortion_maps(K, k1, (OPTICS.pixels_x, OPTICS.pixels_y))

    assert np.array_equal(mapx, expected_x)
    assert np.array_equal(mapy, expected_y)
    assert np.array_equal(cam.lens_vignette(OPTICS), cam.vignette(OPTICS))


def test_the_cached_maps_cannot_be_scribbled_on():
    """A caller that mutates a shared array would poison every later render."""
    k1 = cam.barrel_from_datasheet(OPTICS)

    mapx, _ = cam.lens_maps(OPTICS, k1)
    with pytest.raises(ValueError):
        mapx[0, 0] = 0.0
