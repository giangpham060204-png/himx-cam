"""The camera layout description, checked without a running Slicer."""

import pathlib
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from targeting import layouts  # noqa: E402


def _views():
    return ET.fromstring(layouts.camera_layout_xml()).iter("view")


def test_layout_id_is_not_a_builtin():
    # Slicer keeps ids below 100 for its own arrangements.
    assert layouts.CAMERA_LAYOUT_ID >= 100


def test_three_camera_views_then_the_3d_view():
    found = [(view.get("class"), view.get("singletontag"))
             for view in _views()]
    assert found == [
        ("vtkMRMLSliceNode", "Cam1"),
        ("vtkMRMLSliceNode", "Cam2"),
        ("vtkMRMLSliceNode", "Cam3"),
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


def test_the_standard_views_are_not_reused():
    tags = {view.get("singletontag") for view in _views()}
    assert tags.isdisjoint({"Red", "Green", "Yellow", "Plate"})


def test_the_layout_parses_as_one_root_language():
    root = ET.fromstring(layouts.camera_layout_xml())
    assert root.tag == "layout"
    assert root.get("type") in ("vertical", "horizontal")
    # Two panes at the top level: the camera row and the 3D view.
    assert len(list(root)) == 2
