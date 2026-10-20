"""The whole chain, in one test, with no human anywhere.

This is the flow the spec exists for: nominate, solve, record, read back. It
runs the exact sequence that would otherwise be clicked by hand.
"""

import json
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
from targeting.camera_log import read_camera_targets  # noqa: E402
from targeting.clicks import CLICKS_FILE  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402

OPTICS = optics_for()


def build(seed=11, sources=None):
    root = pathlib.Path(__file__).resolve().parents[1]
    return camera_sandbox.Sandbox(
        optics=OPTICS,
        geometry=camera_sandbox.RigGeometry(spread=150.0, standoff=500.0,
                                            aim_offset=35.0, marker_mm=40.0),
        rig=Rig(), frame=Frame.load(root / "frame.json"), seed=seed, sources=sources,
    )


def test_the_flow_from_a_click_to_a_readable_record(tmp_path):
    box = build()
    box.render_all()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))

    outcome = box.nominate(0, target[0], target[1])
    assert outcome.status == "found"

    path = box.place(tmp_path)
    log = read_camera_targets(path)

    assert log.problems == ()
    assert len(log.targets) == 1
    recorded = log.targets[0]
    assert recorded.cube == pytest.approx(outcome.cube, abs=0.01)
    assert recorded.ras == pytest.approx(outcome.ras, abs=0.01)
    assert len(recorded.pixels) == 3


def test_the_answer_does_not_depend_on_where_the_frames_came_from():
    """The seam, proved: stored frames and freshly rendered ones must agree.

    If anything above frame_sources ever grows a dependency on where pictures
    come from, this goes red.
    """
    rendered_box = build(seed=4)
    rendered_box.render_all()
    stored = [frame.copy() for frame in rendered_box.frames]

    # the same geometry, fed by a genuinely different implementation of the
    # source -- not by the renderer at all
    replayed_box = build(seed=4,
                         sources=[frame_sources.StoredFrames([frame]) for frame in stored])
    replayed_box.render_all()

    target = camera_sandbox.where_it_lands(rendered_box.cameras[0], (0.0, 0.0, 0.0))
    rendered = rendered_box.nominate(0, target[0], target[1])
    replayed = replayed_box.nominate(0, target[0], target[1])

    assert rendered.status == "found" and replayed.status == "found"
    assert replayed.cube == pytest.approx(rendered.cube, abs=1e-6)


def test_a_sandbox_handed_frames_cannot_claim_to_know_the_truth():
    """It did not place the marker, so it must not report how far off it was."""
    rendered_box = build(seed=4)
    rendered_box.render_all()
    stored = [frame.copy() for frame in rendered_box.frames]
    box = build(seed=4, sources=[frame_sources.StoredFrames([frame]) for frame in stored])
    box.render_all()
    target = camera_sandbox.where_it_lands(box.cameras[0], (0.0, 0.0, 0.0))

    outcome = box.nominate(0, target[0], target[1])

    assert outcome.status == "found"
    assert outcome.error_mm is None
    assert "against truth" not in box.readout()
    assert "truth unknown" in box.readout()


def test_the_pictures_slicer_shows_solve_to_the_marker_they_draw(tmp_path):
    """Frames written for Slicer, clicked at the marker, solved back to it.

    The clicks are produced by the same projection that solves, so this pins the
    interface rather than the optics: the three pixels must be read in camera
    order, or the point lands somewhere else entirely.
    """
    code = camera_sandbox.main(["--write-frames", str(tmp_path)])
    assert code == 0
    box = build(seed=4)
    box.render_all()
    pixels = [camera_sandbox.where_it_lands(camera, np.array([[0.0, 0.0, 0.0]]))
              for camera in box.cameras]
    (tmp_path / CLICKS_FILE).write_text(
        json.dumps({"pixels": [list(map(float, pixel)) for pixel in pixels]}),
        encoding="utf-8")

    applied, _ = box.follow_clicks(tmp_path)

    assert applied is True
    assert box.outcome.cube == pytest.approx((50.0, 50.0, 50.0), abs=2.0)


def test_live_clicks_solve_only_once_the_set_is_whole(tmp_path):
    """Each camera click syncs on its own; the point lands with the third."""
    box = build(seed=4)
    box.render_all()
    pixels = [camera_sandbox.where_it_lands(camera, np.array([[0.0, 0.0, 0.0]]))
              for camera in box.cameras]
    live = [[float(value) for value in pixel] for pixel in pixels]

    (tmp_path / CLICKS_FILE).write_text(
        json.dumps({"pixels": [live[0], None, None]}), encoding="utf-8")
    applied, note = box.follow_clicks(tmp_path)
    assert applied is True
    assert box.outcome is None
    assert "waiting" in note

    (tmp_path / CLICKS_FILE).write_text(
        json.dumps({"pixels": [live[0], live[1], None]}), encoding="utf-8")
    applied, _ = box.follow_clicks(tmp_path)
    assert applied is True
    assert box.outcome is None

    (tmp_path / CLICKS_FILE).write_text(
        json.dumps({"pixels": live}), encoding="utf-8")
    applied, _ = box.follow_clicks(tmp_path)

    assert applied is True
    assert box.outcome.status == "found"
    assert box.outcome.found_by == "manual"
    assert box.outcome.cube == pytest.approx((50.0, 50.0, 50.0), abs=2.0)
