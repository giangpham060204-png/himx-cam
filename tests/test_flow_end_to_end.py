"""The whole chain, in one test, with no human anywhere.

This is the flow the spec exists for: nominate, solve, record, read back. It
runs the exact sequence that would otherwise be clicked by hand.
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
from targeting.camera_log import read_camera_targets  # noqa: E402
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
