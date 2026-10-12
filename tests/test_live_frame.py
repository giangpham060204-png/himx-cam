"""Tests for the Slicer bridge, using a fake `slicer` module.

The bridge is the only code that touches Slicer. These tests confirm the
widget mapping (Red=XY axial, Green=XZ coronal, Yellow=YZ sagittal), that the
live matrix is passed through untouched, and that live_frame() yields a Frame
the converter can use. The convention check against a real Slicer is done by
tools/check_live_mapping.py, which runs inside Slicer.

Matrix fixtures are the exact `GetXYToRAS()` values of a default Slicer
5.12.3 four-up layout (see tests/test_view_mapping.py for the conventions).
"""

import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from slicer_bridge import live_frame as bridge  # noqa: E402
from targeting.converter import convert  # noqa: E402

S = 250.0 / 137.0

RED_MATRIX = [
    [-S, 0.0, 0.0, S * 149.0],
    [0.0, S, 0.0, -S * 68.5],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]
GREEN_MATRIX = [
    [-S, 0.0, 0.0, S * 149.5],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, S, 0.0, -S * 68.5],
    [0.0, 0.0, 0.0, 1.0],
]
YELLOW_MATRIX = [
    [0.0, 0.0, -1.0, 0.0],
    [-S, 0.0, 0.0, S * 149.0],
    [0.0, S, 0.0, -S * 68.5],
    [0.0, 0.0, 0.0, 1.0],
]
IDENTITY = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]


class _FakeMatrix:
    def __init__(self, rows):
        self._rows = rows

    def GetElement(self, row, col):
        return self._rows[row][col]


class _FakeSliceNode:
    def __init__(self, xy_to_ras, slice_to_ras, dims=(298, 137, 1)):
        self._xy_to_ras = _FakeMatrix(xy_to_ras)
        self._slice_to_ras = _FakeMatrix(slice_to_ras)
        self._dims = list(dims)

    def GetXYToRAS(self):
        return self._xy_to_ras

    def GetSliceToRAS(self):
        return self._slice_to_ras

    def GetDimensions(self):
        return self._dims


class _FakeSliceView:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class _FakeSliceLogic:
    def __init__(self, node):
        self._node = node

    def GetSliceNode(self):
        return self._node


class _FakeSliceWidget:
    def __init__(self, node, width, height):
        self._node = node
        self._view = _FakeSliceView(width, height)

    def sliceLogic(self):
        return _FakeSliceLogic(self._node)

    def sliceView(self):
        return self._view


class _FakeLayout:
    def __init__(self, widgets):
        self._widgets = widgets

    def sliceWidget(self, name):
        return self._widgets.get(name)


def _widget(matrix):
    return _FakeSliceWidget(_FakeSliceNode(matrix, IDENTITY), 298, 137)


def _layout(include=("Red", "Green", "Yellow")):
    all_widgets = {
        "Red": _widget(RED_MATRIX),
        "Green": _widget(GREEN_MATRIX),
        "Yellow": _widget(YELLOW_MATRIX),
    }
    return _FakeLayout({name: all_widgets[name] for name in include})


def _install_fake_slicer(monkeypatch, layout):
    fake = types.SimpleNamespace(
        app=types.SimpleNamespace(layoutManager=lambda: layout)
    )
    monkeypatch.setitem(sys.modules, "slicer", fake)


def test_widgets_map_to_views_coronal_is_green(monkeypatch):
    layout = _layout()
    _install_fake_slicer(monkeypatch, layout)
    data = bridge.read_view_data(layout=layout)
    assert set(data) == {"XY", "XZ", "YZ"}
    # Green's matrix is the coronal one: its column 1 moves along S.
    assert data["XZ"]["xy_to_ras"][2][1] == pytest.approx(S)


def test_read_view_data_passes_sizes_and_origin_through(monkeypatch):
    layout = _layout()
    _install_fake_slicer(monkeypatch, layout)
    data = bridge.read_view_data(layout=layout)
    assert data["XY"]["width_px"] == 298.0
    assert data["XY"]["qt_width_px"] == 298.0
    assert data["XY"]["slice_origin_ras"] == [0.0, 0.0, 0.0]


def test_live_frame_matches_the_verified_slicer_orientations(monkeypatch):
    layout = _layout()
    _install_fake_slicer(monkeypatch, layout)
    frame = bridge.live_frame()
    assert frame.view_axis_map["XY"]["v"] == ("A", -1)
    assert frame.view_axis_map["XZ"]["v"] == ("S", -1)
    assert frame.view_axis_map["XZ"]["depth"] == ("A", 1)
    assert frame.view_axis_map["YZ"]["depth"] == ("R", -1)


def test_live_frame_is_usable_by_the_converter(monkeypatch):
    layout = _layout()
    _install_fake_slicer(monkeypatch, layout)
    frame = bridge.live_frame()
    cu, cv = frame.view_center_px["YZ"]
    ahead = convert("YZ", cu, cv, 27.0, frame)
    assert ahead[0] == pytest.approx(50.0 - 27.0)
    # Screen-right in the sagittal view moves toward A-negative (nose is left).
    right = convert("YZ", cu + 10.0, cv, 0.0, frame)
    assert right[1] == pytest.approx(50.0 - 10.0 * S)


def test_live_frame_rebuilds_from_the_current_window_each_call(monkeypatch):
    """No caching: a resized window must be picked up on the next call."""
    layout = _layout()
    _install_fake_slicer(monkeypatch, layout)
    first = bridge.live_frame()

    # Simulate a wider window: same scale, wider matrix.
    layout._widgets["Red"]._view = _FakeSliceView(500, 137)
    layout._widgets["Red"]._node = _FakeSliceNode(
        [
            [-S, 0.0, 0.0, S * 250.0],
            [0.0, S, 0.0, -S * 68.5],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        IDENTITY,
        dims=(500, 137, 1),
    )

    second = bridge.live_frame()
    assert first.view_center_px["XY"] == pytest.approx((149.0, 67.5))
    assert second.view_center_px["XY"] == pytest.approx((250.0, 67.5))
    assert second.pixel_size_mm["XY"] == pytest.approx(250.0 / 137.0)


def test_a_missing_slice_view_is_reported_by_name(monkeypatch):
    layout = _layout(include=("Red", "Green"))
    _install_fake_slicer(monkeypatch, layout)
    with pytest.raises(bridge.MissingSliceView, match="Yellow"):
        bridge.read_view_data(layout=layout)


def test_only_the_bridge_imports_slicer():
    """The pure layer must stay importable on this laptop, with no Slicer."""
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "targeting").glob("*.py"):
        if "import slicer" in path.read_text():
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
    bridge_source = (root / "slicer_bridge" / "live_frame.py").read_text()
    assert "import slicer" in bridge_source
