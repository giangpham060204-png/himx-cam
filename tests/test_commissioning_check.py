"""The tool that compares a triangulated point with a measured distance.

It is the only check in this work that can catch a systematic error: three lines
of sight can meet perfectly and still be wrong together.
"""

import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

cv2 = pytest.importorskip("cv2")

import commissioning_check as check  # noqa: E402
from targeting.optics import optics_for  # noqa: E402


def test_a_simulated_run_round_trips_a_known_distance():
    """Render, detect, triangulate, measure, compare -- and PASS."""
    code = check.main(["--simulate", "--instants", "3", "--tolerance-mm", "1.0"])

    assert code == 0


def test_the_fail_path_exits_one():
    """No measurement is perfect, so a tolerance of zero must fail."""
    code = check.main(["--simulate", "--instants", "2", "--tolerance-mm", "0.0"])

    assert code == 1


def test_a_simulated_run_refuses_a_tape_value_it_does_not_need():
    """A simulated run knows the distance; silently ignoring the flag would lie."""
    code = check.main(["--simulate", "--measured-mm", "500"])

    assert code == 2


def test_the_frames_input_does_not_exist_yet():
    """Real pictures need calibrated camera poses, so the flag must not pretend."""
    with pytest.raises(SystemExit):
        check.main(["--frames", "/tmp/opencode", "--measured-mm", "500"])


def test_the_summary_says_which_way_the_error_leans():
    points = [(1.0, 0.0, 0.0), (1.2, 0.0, 0.0), (0.8, 0.0, 0.0)]

    summary = check.summarise(points, rig_origin=(0.0, 0.0, 0.0), measured_mm=1.0)

    assert summary.runs == 3
    assert summary.bias_mm == pytest.approx(0.0, abs=1e-9)
    assert summary.worst_mm == pytest.approx(0.2, abs=1e-9)


def test_the_tape_reference_is_the_camera_centroid_not_the_nominal_standoff():
    """The three cameras stand at three heights; the nominal number is not the distance."""
    origins = check.camera_origins([p for p in check.simulated_projections(standoff_mm=500.0)])

    centroid = origins.mean(axis=0)

    assert centroid[2] > 500.0, "the cameras do not all stand at the nominal standoff"
    assert check.scene_truth(origins) == pytest.approx(
        float(np.linalg.norm(centroid)), abs=1e-9)


def test_an_uneven_frame_set_is_refused_not_silently_subset():
    """Two frames for three cameras must refuse: a subset would measure the wrong rig."""
    optics = optics_for()
    projections = check.simulated_projections(standoff_mm=500.0, optics=optics)
    box = check.simulated_scene(optics, 500.0)
    images = box.render_all()[:2]

    with pytest.raises(check.Refused, match="one frame per camera"):
        check.measure_frames(images, optics, projections)


def test_zero_instants_is_refused_with_exit_two():
    assert check.main(["--simulate", "--instants", "0"]) == 2
