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
import synthetic_camera as cam  # noqa: E402
from targeting.converter import OutOfFieldError, check_in_field_ras  # noqa: E402
from targeting.frame import Frame  # noqa: E402
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
from matplotlib.backend_bases import MouseEvent  # noqa: E402


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
