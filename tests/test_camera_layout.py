"""The camera layout description, checked without a running Slicer."""

import pathlib
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from targeting import layouts  # noqa: E402


def _views():
    return list(ET.fromstring(layouts.camera_layout_xml()).iter("view"))


def test_layout_id_is_not_a_builtin():
    # Slicer keeps ids below 100 for its own arrangements.
    assert layouts.CAMERA_LAYOUT_ID >= 100


def test_camera_column_then_the_standard_four_up():
    found = [(view.get("class"), view.get("singletontag"))
             for view in _views()]
    assert found == [
        ("vtkMRMLSliceNode", "Cam1"),
        ("vtkMRMLSliceNode", "Cam2"),
        ("vtkMRMLSliceNode", "Cam3"),
        ("vtkMRMLSliceNode", "Red"),
        ("vtkMRMLSliceNode", "Green"),
        ("vtkMRMLSliceNode", "Yellow"),
        ("vtkMRMLViewNode", "1"),
    ]


def test_each_camera_pane_is_labelled_and_coloured():
    for view, label, colour in zip(_views(), layouts.CAMERA_VIEW_LABELS,
                                   layouts.CAMERA_VIEW_COLOURS):
        properties = {entry.get("name"): entry.text
                      for entry in view.iter("property")}
        assert properties["viewlabel"] == label
        assert properties["viewcolor"] == colour
        assert properties["orientation"] == "Axial"


def test_the_standard_views_keep_their_orientations():
    by_name = {view.get("singletontag"): view for view in _views()}
    for name, _label, orientation in layouts.STANDARD_SLICE_VIEWS:
        properties = {entry.get("name"): entry.text
                      for entry in by_name[name].iter("property")}
        assert properties["orientation"] == orientation


def test_every_view_appears_once():
    tags = [view.get("singletontag") for view in _views()]
    assert len(tags) == len(set(tags))
    # Three cameras and the four standard panes: nothing was traded away.
    assert set(tags) == {"Cam1", "Cam2", "Cam3", "Red", "Green", "Yellow",
                         "1"}


def test_the_layout_parses_as_one_root_language():
    root = ET.fromstring(layouts.camera_layout_xml())
    assert root.tag == "layout"
    assert root.get("type") in ("vertical", "horizontal")
    # Two panes at the top level: the camera column and the four-up.
    assert len(list(root)) == 2
