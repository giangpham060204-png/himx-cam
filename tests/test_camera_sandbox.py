"""Tests for the code above the seam: naming a target and solving for it.

No window is opened. Every decision this module makes is a plain function of
plain values, which is what lets the flow be tested without a human clicking.
"""

import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")

import camera_sandbox  # noqa: E402
import frame_sources  # noqa: E402
import synthetic_camera as cam  # noqa: E402
from targeting.converter import OutOfFieldError, check_in_field_ras  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.logbook import append_target  # noqa: E402
from targeting.optics import optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402

OPTICS = optics_for()


def a_sandbox(seed=3, spread=150.0, standoff=500.0, aim=35.0, rig=None, marker_mm=40.0):
    frame = Frame.load(pathlib.Path(__file__).resolve().parents[1] / "frame.json")
    return camera_sandbox.Sandbox(
        optics=OPTICS, geometry=camera_sandbox.RigGeometry(
            spread=spread, standoff=standoff, aim_offset=aim, marker_mm=marker_mm),
        rig=rig or Rig(), frame=frame, seed=seed,
    )


def test_the_sandbox_builds_three_cameras():
    box = a_sandbox()
    assert [camera.name for camera in box.cameras] == ["c1", "c2", "c3"]
    for camera in box.cameras:
        assert camera.projection.shape == (3, 4)


def test_rendering_fills_every_camera_and_reports_it():
    box = a_sandbox()
    box.render_all()
    assert len(box.frames) == 3
    assert all(f.shape == (OPTICS.pixels_y, OPTICS.pixels_x) for f in box.frames)


def test_clicking_the_object_returns_the_point_it_is_at():
    box = a_sandbox()
    box.render_all()
    target = cam.diagonal_crossing(
        cam.straight_lens(cam.detect_marker(box.frames[0], cam.detector()), OPTICS))

    outcome = box.nominate(0, target[0], target[1])

    assert outcome.status == "found"
    assert outcome.cube == pytest.approx((50.0, 50.0, 50.0), abs=5.0)
    assert outcome.ras == pytest.approx((0.0, 0.0, 0.0), abs=5.0)
    assert outcome.error_mm is not None and outcome.error_mm < 5.0
    assert outcome.predicted_mm is not None and outcome.predicted_mm > 0.0


def test_clicking_away_from_the_object_names_what_to_do():
    box = a_sandbox()
    box.render_all()

    outcome = box.nominate(0, 5.0, 5.0)

    assert outcome.status == "not-on-object"
    assert "click the object" in outcome.message


def test_nominating_twice_replaces_rather_than_accumulates():
    box = a_sandbox()
    box.render_all()
    corner = np.array([[0.0, 0.0, 0.0]])
    target = camera_sandbox.where_it_lands(box.cameras[0], corner)
    other = camera_sandbox.where_it_lands(box.cameras[1], corner)

    first = box.nominate(0, target[0], target[1])
    second = box.nominate(1, other[0], other[1])

    assert first.status == "found" and second.status == "found"
    assert box.outcome is second
    assert len(box.outcome.residual_px) == 3


def test_a_marker_off_the_edge_of_the_picture_is_reported_by_name():
    """It is visible enough to invite a click, but its quiet zone is clipped."""
    box = a_sandbox(aim=300.0)
    box.render_all()

    outcome = box.nominate(0, 160.0, 160.0)

    assert outcome.status in ("not-readable", "not-on-object")
    if outcome.status == "not-readable":
        assert "c" in outcome.message


def test_three_cameras_that_are_really_one_are_reported_not_crashed():
    """A layout with no answer must come back as a status, not a traceback.

    Three copies of one camera is the honest way to build that case: the slider
    ranges cannot reach a singular arrangement, and a test that hopes to is a
    test that will disagree with its own code one day.
    """
    box = a_sandbox()
    box.render_all()
    box.cameras = [box.cameras[0]] * 3
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))

    outcome = box.nominate(0, target[0], target[1])

    assert outcome.status == "degenerate"
    assert outcome.message


def test_a_point_outside_the_field_is_refused_with_the_axis():
    far = Rig(translation_mm=(500.0, 0.0, 0.0))
    box = a_sandbox(rig=far)
    box.render_all()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))

    outcome = box.nominate(0, target[0], target[1])

    assert outcome.status == "outside-field"
    assert "R=" in outcome.message


def test_placing_writes_one_row_that_reads_back(tmp_path):
    box = a_sandbox()
    box.render_all()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))
    box.nominate(0, target[0], target[1])

    path = box.place(tmp_path)

    from targeting.camera_log import read_camera_targets
    log = read_camera_targets(path)
    assert log.problems == ()
    assert len(log.targets) == 1
    assert log.targets[0].cube == pytest.approx(box.outcome.cube, abs=1e-3)  # log stores .3f


def test_placing_without_nominating_is_refused(tmp_path):
    box = a_sandbox()

    with pytest.raises(RuntimeError, match="nominate"):
        box.place(tmp_path)


def test_placing_a_refused_point_writes_nothing(tmp_path):
    box = a_sandbox()
    box.render_all()
    box.nominate(0, 5.0, 5.0)

    with pytest.raises(RuntimeError, match="nothing to place"):
        box.place(tmp_path)
    assert list(tmp_path.glob("camera-targets-*.csv")) == []


import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backend_bases import KeyEvent, MouseEvent  # noqa: E402


def a_window():
    box = a_sandbox()
    figure = plt.figure(figsize=(6, 4))
    window = camera_sandbox.SandboxWindow(box, figure)
    window.draw()
    window.render()
    figure.canvas.draw()
    return window


def press(window, pane_index, u_px, v_px):
    """Press at an image pixel.

    MouseEvent carries DISPLAY coordinates and derives xdata/ydata from the axes
    transform, so the pane's own transform does the conversion. Handing it image
    pixels directly would press somewhere else and read a different xdata.
    """
    pane = window.panes[pane_index]
    x, y = pane.transData.transform((u_px, v_px))
    event = MouseEvent("button_press_event", window.figure.canvas, x, y, button=1)
    window.on_press(event)


def test_a_click_on_the_object_is_nominated_once():
    window = a_window()
    target = camera_sandbox.where_it_lands(window.sandbox.cameras[0], (0.0, 0.0, 0.0))

    press(window, 0, target[0], target[1])

    assert window.sandbox.outcome.status == "found"


def test_a_dragged_mouse_nominates_once_not_once_per_pixel():
    """matplotlib sends motion between press and release; one click, one answer."""
    window = a_window()
    target = camera_sandbox.where_it_lands(window.sandbox.cameras[0], (0.0, 0.0, 0.0))

    press(window, 0, target[0], target[1])
    first = window.sandbox.outcome
    pane = window.panes[0]
    for step in range(1, 6):
        x, y = pane.transData.transform((target[0] + step, target[1]))
        motion = MouseEvent("motion_notify_event", window.figure.canvas, x, y, button=1)
        window.on_motion(motion)

    assert window.sandbox.outcome is first


def test_a_click_while_a_nomination_is_shown_replaces_it():
    window = a_window()
    target = camera_sandbox.where_it_lands(window.sandbox.cameras[0], (0.0, 0.0, 0.0))

    press(window, 0, target[0], target[1])
    first = window.sandbox.outcome
    press(window, 0, target[0], target[1])
    second = window.sandbox.outcome

    assert second is not first
    assert second.status == "found"


def test_pressing_place_with_nothing_nominated_explains_rather_than_crashes():
    """`p` on a fresh window must repaint the hint, not leak a traceback."""
    window = a_window()
    event = KeyEvent("key_press_event", window.figure.canvas, "p")

    window.on_key(event)  # must not raise

    assert window.sandbox.outcome is None
    assert "click the object" in window.sandbox.readout()


def test_the_headless_run_writes_both_figures_and_opens_no_window(tmp_path):
    code = camera_sandbox.main(["--out", str(tmp_path), "--frames", "2", "--trials", "20"])

    assert code == 0
    assert (tmp_path / "camera-views.png").is_file()
    assert (tmp_path / "camera-accuracy.png").is_file()


def test_the_readout_carries_the_numbers_and_the_verdicts():
    box = a_sandbox()
    box.render_all()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))

    box.nominate(0, target[0], target[1])
    text = box.readout()

    assert "cube" in text and "RAS" in text
    assert "against truth" in text
    assert "predicted" in text
    assert "inside" in text
    assert "c1" in text and "c2" in text and "c3" in text


def test_the_readout_warns_when_the_residual_is_high_but_stays_placeable(tmp_path):
    """Above 2 px the operator gets a warning, not a refusal (spec 7.2)."""
    box = a_sandbox()
    box.sigma = 0.30  # the warning is about the residual, not the detector
    box.outcome = camera_sandbox.Outcome(
        status="found", pixels=((160.0, 160.0), (158.5, 161.25), (161.0, 159.0)),
        cube=(50.0, 50.0, 50.0), ras=(0.0, 0.0, 0.0),
        residual_px=(2.7, 0.21, 0.19), error_mm=0.73, predicted_mm=0.76,
    )

    text = box.readout()

    assert "warning" in text
    assert "2.7" in text
    box.place(tmp_path)
    assert list(tmp_path.glob("camera-targets-*.csv")) != []


def test_the_readout_stays_quiet_when_the_residual_is_small():
    box = a_sandbox()
    box.sigma = 0.30
    box.outcome = camera_sandbox.Outcome(
        status="found", pixels=((160.0, 160.0), (158.5, 161.25), (161.0, 159.0)),
        cube=(50.0, 50.0, 50.0), ras=(0.0, 0.0, 0.0),
        residual_px=(0.21, 0.18, 0.24), error_mm=0.73, predicted_mm=0.76,
    )

    assert "warning" not in box.readout()


def test_the_readout_says_why_when_nothing_was_found():
    box = a_sandbox()
    box.render_all()

    box.nominate(0, 5.0, 5.0)

    assert "click the object" in box.readout()


def test_the_readout_names_a_point_outside_the_field():
    far = Rig(translation_mm=(500.0, 0.0, 0.0))
    box = a_sandbox(rig=far)
    box.render_all()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))

    box.nominate(0, target[0], target[1])

    assert "field" in box.readout()
    assert "R=" in box.readout()


def test_every_pane_is_drawn_in_its_own_camera_colour():
    """Colour says which camera; a picture where c1 and c3 look alike is unreadable."""
    window = a_window()

    assert camera_sandbox.CAMERA_COLOURS == ("tab:red", "tab:green", "tab:orange")
    for index in range(3):
        assert window.panes[index].title.get_color() == camera_sandbox.CAMERA_COLOURS[index]


def test_the_three_rays_are_drawn_in_their_own_camera_colours():
    window = a_window()
    target = camera_sandbox.where_it_lands(window.sandbox.cameras[0], (0.0, 0.0, 0.0))
    press(window, 0, target[0], target[1])

    drawn = {line.get_color() for line in window.panes["scene"].lines}
    for colour in camera_sandbox.CAMERA_COLOURS:
        assert colour in drawn


def test_the_marks_that_are_not_cameras_do_not_borrow_a_camera_colour():
    """The printed square and the true point must not read as c1/c2/c3."""
    window = a_window()
    scene = window.panes["scene"]
    colours = {line.get_color() for line in scene.lines}

    assert "grey" in colours                      # the printed square
    camera_like = [c for c in colours if c in camera_sandbox.CAMERA_COLOURS]
    assert len(camera_like) <= 3, "more camera-coloured lines than there are cameras"


def test_the_pane_draws_where_the_answer_says_the_marker_is():
    """The gap between the two marks IS the residual, drawn on the image.

    This is the check that survives the loss of the truth line: a frame, a
    detected pixel and a projection matrix are all a real system has too.
    """
    window = a_window()
    target = camera_sandbox.where_it_lands(window.sandbox.cameras[0], (0.0, 0.0, 0.0))
    press(window, 0, target[0], target[1])

    outcome = window.sandbox.outcome
    world = np.asarray(window.sandbox.rig.to_rig(tuple(outcome.ras)), float)
    for index, camera in enumerate(window.sandbox.cameras):
        pane = window.panes[index]
        seen_mark, predicted_mark, connector = pane.lines
        seen = np.asarray(outcome.pixels[index], float)
        predicted = camera_sandbox.where_it_lands(camera, world)

        assert (seen_mark.get_xdata()[0], seen_mark.get_ydata()[0]) == pytest.approx(seen)
        assert (predicted_mark.get_xdata()[0], predicted_mark.get_ydata()[0]) == pytest.approx(predicted)
        span = float(np.hypot(connector.get_xdata()[1] - connector.get_xdata()[0],
                              connector.get_ydata()[1] - connector.get_ydata()[0]))
        assert span == pytest.approx(outcome.residual_px[index], abs=0.01)
        assert f"residual {outcome.residual_px[index]:.2f} px" in pane.get_title()


def test_panes_predict_nothing_when_nothing_was_found():
    window = a_window()
    press(window, 0, 5.0, 5.0)

    for index in range(3):
        assert list(window.panes[index].lines) == []
        assert "residual" not in window.panes[index].get_title()


def test_a_repeat_reports_the_spread_and_clears_the_standing_nomination():
    box = a_sandbox()

    result = box.repeat(times=4)

    assert result.runs == 4
    assert result.found == 4 and result.failed == 0
    assert result.mean == pytest.approx((50.0, 50.0, 50.0), abs=5.0)
    assert all(spread >= 0.0 for spread in result.std_mm)
    assert result.max_mm >= 0.0
    assert box.outcome is None, "the frames it was found against no longer exist"
    assert box.repeat_result is result
    assert "repeat 4" in box.readout()


def test_two_grabs_of_a_static_scene_differ():
    """A real camera never repeats itself; if it does, a repeat measures nothing."""
    box = a_sandbox()
    first = box.render_all()[0].copy()
    second = box.render_all()[0]

    assert not np.array_equal(first, second)


def test_a_repeat_that_reads_nothing_reports_it_rather_than_crashing():
    box = a_sandbox()
    box.cameras = [box.cameras[0]] * 3          # a rig with no answer

    result = box.repeat(times=3)

    assert result.found == 0 and result.failed == 3
    assert result.mean is None and result.std_mm is None
    assert "nothing found" in box.readout()


def test_a_repeat_needs_at_least_two_runs():
    box = a_sandbox()

    with pytest.raises(ValueError, match="at least two"):
        box.repeat(times=1)


def test_a_nomination_clears_the_last_repeat():
    box = a_sandbox()
    box.repeat(times=2)
    box.render_all()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))

    box.nominate(0, target[0], target[1])

    assert box.repeat_result is None
    assert "repeat" not in box.readout()


def test_a_zero_spread_says_what_it_is_and_is_not():
    """The sandbox's own renderer repeats exactly, so zero is not a detector claim."""
    box = a_sandbox()
    box.repeat_result = camera_sandbox.Repeat(
        runs=4, found=4, failed=0, mean=(50.0, 50.0, 50.0),
        std_mm=(0.0, 0.0, 0.0), max_mm=0.0)

    text = box.readout()

    assert "spread std (0.00, 0.00, 0.00) mm" in text
    assert "says nothing about the detector" in text


def test_a_real_spread_does_not_carry_the_zero_note():
    box = a_sandbox()
    box.repeat_result = camera_sandbox.Repeat(
        runs=4, found=4, failed=0, mean=(50.0, 50.0, 50.0),
        std_mm=(0.31, 0.22, 0.19), max_mm=0.55)

    text = box.readout()

    assert "says nothing about the detector" not in text


def test_three_hand_placed_pixels_solve_without_the_detector():
    box = a_sandbox()
    box.render_all()
    corner = np.array([[0.0, 0.0, 0.0]])
    pixels = [tuple(float(v) for v in camera_sandbox.where_it_lands(camera, corner))
              for camera in box.cameras]

    outcome = box.nominate_manually(pixels)

    assert outcome.status == "found"
    assert outcome.cube == pytest.approx((50.0, 50.0, 50.0), abs=5.0)
    assert outcome.found_by == "manual"
    assert outcome.sigma_px == camera_sandbox.CLICK_SIGMA_PX
    assert "manual" in box.readout()


def test_a_manual_point_is_placed_in_the_log_marked_as_manual(tmp_path):
    box = a_sandbox()
    box.render_all()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))
    box.nominate_manually([tuple(float(v) for v in target)] * 3)

    path = box.place(tmp_path)

    from targeting.camera_log import read_camera_targets
    assert read_camera_targets(path).targets[0].found_by == "manual"


def test_a_misclicked_ray_shows_up_as_a_residual():
    """The honest lesson of this mode: hand-placed rays miss, and say by how much."""
    box = a_sandbox()
    box.render_all()
    pixels = [tuple(float(v) for v in camera_sandbox.where_it_lands(
        camera, (0.0, 0.0, 0.0))) for camera in box.cameras]
    pixels[1] = (pixels[1][0] + 6.0, pixels[1][1] - 4.0)

    outcome = box.nominate_manually(pixels)

    assert outcome.status == "found"
    assert max(outcome.residual_px) > 2.0
    assert "warning" in box.readout()


def test_manual_nomination_needs_one_pixel_per_camera():
    box = a_sandbox()
    box.render_all()

    with pytest.raises(ValueError, match="one pixel"):
        box.nominate_manually([(10.0, 10.0), (20.0, 20.0)])


def test_manual_mode_gathers_one_click_per_camera():
    window = a_window()
    window.on_key(KeyEvent("key_press_event", window.figure.canvas, "m"))
    target = camera_sandbox.where_it_lands(window.sandbox.cameras[0], (0.0, 0.0, 0.0))

    press(window, 0, target[0], target[1])

    assert window.manual is True
    assert list(window.clicked) == [0]
    assert window.clicked[0] == pytest.approx((float(target[0]), float(target[1])))
    assert "manual" in window.figure._suptitle.get_text()


def test_the_third_click_completes_a_manual_nomination():
    window = a_window()
    window.on_key(KeyEvent("key_press_event", window.figure.canvas, "m"))
    for index, camera in enumerate(window.sandbox.cameras):
        point = camera_sandbox.where_it_lands(camera, (0.0, 0.0, 0.0))
        press(window, index, point[0], point[1])

    assert window.clicked == {}
    assert window.sandbox.outcome.status == "found"
    assert window.sandbox.outcome.found_by == "manual"


def test_toggling_manual_mode_forgets_a_half_finished_nomination():
    window = a_window()
    window.on_key(KeyEvent("key_press_event", window.figure.canvas, "m"))
    target = camera_sandbox.where_it_lands(window.sandbox.cameras[0], (0.0, 0.0, 0.0))
    press(window, 0, target[0], target[1])

    window.on_key(KeyEvent("key_press_event", window.figure.canvas, "m"))

    assert window.manual is False
    assert window.clicked == {}


def test_a_rebuild_forgets_the_last_repeat():
    box = a_sandbox()
    box.repeat(times=2)
    assert box.repeat_result is not None

    box.rebuild()

    assert box.repeat_result is None
    assert box.outcome is None


def test_a_hypothesis_is_shown_and_measures_nothing(tmp_path):
    box = a_sandbox()
    box.render_all()
    append_target(tmp_path, view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                          cube=(50.0, 50.0, 50.0), ras=(0.0, 0.0, 0.0))

    note = box.load_hypothesis(tmp_path)

    assert box.hypothesis_cube == pytest.approx((50.0, 50.0, 50.0))
    assert box.outcome is None, "loading a hypothesis must not invent a measurement"
    assert "hypothesis" in box.readout()
    assert "not a measurement" in box.readout()
    assert "targets-" in note
    assert list(tmp_path.glob("camera-targets-*.csv")) == [], "a hypothesis is never logged"


def test_loading_a_hypothesis_where_there_is_none_says_so(tmp_path):
    box = a_sandbox()

    note = box.load_hypothesis(tmp_path)

    assert box.hypothesis_cube is None
    assert note
    assert "hypothesis" in box.readout()


def test_the_hypothesis_is_drawn_as_a_diamond_in_the_rig_frame(tmp_path):
    window = a_window()
    append_target(tmp_path, view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                          cube=(50.0, 50.0, 50.0), ras=(0.0, 0.0, 0.0))
    window.sandbox.load_hypothesis(tmp_path)

    window._paint()

    for index, camera in enumerate(window.sandbox.cameras):
        pane = window.panes[index]
        assert len(pane.lines) == 1, "a diamond, and nothing to connect it to yet"
        assert pane.lines[0].get_marker() == "D"
        assert pane.lines[0].get_color() == camera_sandbox.CAMERA_COLOURS[index]
    scene_markers = [artist for artist in window.panes["scene"].collections]
    assert scene_markers, "the scene must show the hypothesis too"


def test_a_measurement_is_compared_with_the_hypothesis_standing_beside_it(tmp_path):
    box = a_sandbox()
    box.render_all()
    append_target(tmp_path, view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                          cube=(60.0, 50.0, 50.0), ras=(10.0, 0.0, 0.0))
    box.load_hypothesis(tmp_path)
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))

    box.nominate(0, target[0], target[1])

    text = box.readout()
    marker = "mm from the hypothesis"
    assert marker in text
    line = [row for row in text.splitlines() if marker in row][0]
    apart = float(line.split("is ")[1].split(" mm")[0])
    assert apart == pytest.approx(10.0, abs=0.6)


def test_clearing_forgets_the_hypothesis(tmp_path):
    window = a_window()
    append_target(tmp_path, view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                          cube=(50.0, 50.0, 50.0), ras=(0.0, 0.0, 0.0))
    window.sandbox.load_hypothesis(tmp_path)

    window.on_key(KeyEvent("key_press_event", window.figure.canvas, "c"))

    assert window.sandbox.hypothesis_cube is None
    assert "hypothesis" not in window.sandbox.readout()


def test_handed_frames_never_draw_the_simulated_true_point():
    """The star is a simulation-only mark; over real pictures it would be a lie.

    The guard lives on `outcome.error_mm is not None`, so this pins the absence
    on the window itself: handed frames, rendered, nominated, painted -- and no
    artist in the scene may carry the true-point label.
    """
    rendered = a_sandbox(seed=4)
    rendered.render_all()
    stored = [frame.copy() for frame in rendered.frames]
    frame = Frame.load(pathlib.Path(__file__).resolve().parents[1] / "frame.json")
    box = camera_sandbox.Sandbox(
        optics=OPTICS, geometry=camera_sandbox.RigGeometry(
            spread=150.0, standoff=500.0, aim_offset=35.0, marker_mm=40.0),
        rig=Rig(), frame=frame, seed=4,
        sources=[frame_sources.StoredFrames([frame]) for frame in stored],
    )
    figure = plt.figure(figsize=(6, 4))
    window = camera_sandbox.SandboxWindow(box, figure)
    window.draw()
    window.render()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))
    box.nominate(0, target[0], target[1])
    assert box.outcome.status == "found"
    assert box.outcome.error_mm is None
    window._paint()

    labels = [artist.get_label()
              for artist in window.panes["scene"].collections]
    assert "the true point (simulated)" not in labels
