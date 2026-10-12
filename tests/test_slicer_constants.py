"""Tests for the Slicer-constant extraction logic.

These run without Slicer: the helpers take any object exposing GetElement,
which is what vtkMatrix4x4 does. That is the whole point, so the logic can be
verified on a laptop with no Slicer installed.
"""

import pathlib
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import print_slicer_constants as p  # noqa: E402

BOOTSTRAP_TARGET = pathlib.Path(
    __file__).resolve().parents[1] / "tools" / "measure_now.py"


def _bootstrap():
    """Import measure_now.py under a private name."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("measure_now_under_test", BOOTSTRAP_TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from print_slicer_constants import (  # noqa: E402
    AXIS_NAMES,
    SHOWN_AXES,
    VIEWS,
    Matrix3x4,
    NotSlicerConsole,
    column_axis,
    measure,
    mm_per_pixel,
    slice_normal,
    view_labels,
    viewport_width,
)

# GetXYToRAS matrices as reported by Slicer 5.12.3.
AXIAL = Matrix3x4([[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
CORONAL = Matrix3x4([[-1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]])
SAGITTAL = Matrix3x4([[0.0, 0.0, -1.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


def test_column_not_row_is_the_ijk_direction():
    """Columns, not rows, carry the axis directions.

    GetXYToRAS maps XY pixels to RAS, so COLUMN j is the RAS direction of
    XY axis j. Column 0 is screen-right, column 1 is screen-up, column 2 is
    the slice normal. Reading ROWS happens to look correct for Axial and
    Coronal, because their matrices are diagonal, and silently returns
    garbage for Sagittal, whose matrix is not diagonal.
    """
    assert column_axis(SAGITTAL, 0) == ("A", -1)
    assert column_axis(SAGITTAL, 1) == ("S", 1)
    assert column_axis(SAGITTAL, 2) == ("R", -1)


def test_axial_columns_read_as_the_usual_layout():
    assert column_axis(AXIAL, 0) == ("R", -1)
    assert column_axis(AXIAL, 1) == ("A", 1)
    assert column_axis(AXIAL, 2) == ("S", 1)


def test_coronal_columns_read_as_the_usual_layout():
    assert column_axis(CORONAL, 0) == ("R", -1)
    assert column_axis(CORONAL, 1) == ("S", 1)
    assert column_axis(CORONAL, 2) == ("A", 1)


def test_all_three_measured_views_are_a_permutation_of_ras():
    for matrix in (AXIAL, CORONAL, SAGITTAL):
        axes = sorted(column_axis(matrix, j)[0] for j in range(3))
        assert axes == ["A", "R", "S"]


def test_slice_normal_is_column_two():
    assert slice_normal(AXIAL) == ("S", 1)
    assert slice_normal(CORONAL) == ("A", 1)
    assert slice_normal(SAGITTAL) == ("R", -1)


def test_sagittal_view_now_derives_depth_r():
    """Previously it derived A, i.e. it thought Yellow was Coronal."""
    normal = slice_normal(SAGITTAL)
    frame = p.derive_axis_map(normal, column_axis(SAGITTAL, 0), column_axis(SAGITTAL, 1))
    assert frame["depth"] == ["R", 1]
    assert frame["u"] == ["A", -1]
    assert frame["v"] == ["S", 1]


def test_measure_of_all_three_real_matrices_produces_no_notes(monkeypatch):
    """With columns read correctly, the real layouts raise no concerns."""
    sequence = [AXIAL, CORONAL, SAGITTAL]

    class FakeNode:
        def __init__(self, matrix):
            self.matrix = matrix

        def GetXYToRAS(self):
            return self.matrix

        def GetFieldOfView(self):
            return (250.0, 250.0, 1.0)

    monkeypatch.setattr(p, "_slice_node", lambda *a, **k: FakeNode(sequence.pop(0)))
    monkeypatch.setattr(p, "_viewport_size", lambda *a, **k: 200)
    monkeypatch.setattr(p, "mm_per_pixel", lambda *a, **k: 0.28)
    monkeypatch.setitem(sys.modules, "slicer", types.SimpleNamespace(marker="fake"))

    result = measure()

    assert result["notes"] == []
    assert result["view_axis_map"]["XY"]["u"] == ["R", -1]
    assert result["view_axis_map"]["XZ"]["v"] == ["S", 1]
    assert result["view_axis_map"]["YZ"]["depth"] == ["R", 1]


def test_mm_per_pixel_uses_the_viewport_not_the_image():
    """250 mm across 200 px is 1.25 mm/px; the image size is irrelevant."""
    assert mm_per_pixel(field_of_view_mm=250.0, viewport_px=200) == pytest.approx(1.25)


def test_mm_per_pixel_rejects_a_zero_width_viewport():
    with pytest.raises(ValueError, match="viewport"):
        mm_per_pixel(field_of_view_mm=250.0, viewport_px=0)


def test_measure_uses_viewport_pixels_not_image_dimensions(monkeypatch):
    class FakeNode:
        def __init__(self, matrix):
            self.matrix = matrix

        def GetXYToRAS(self):
            return self.matrix

        def GetFieldOfView(self):
            return (250.0, 250.0, 1.0)

        def GetDimensions(self):
            return (137, 137, 1)  # image size, must NOT be used

    monkeypatch_items = [FakeNode(AXIAL), FakeNode(CORONAL), FakeNode(SAGITTAL)]
    monkeypatch.setattr(p, "_slice_node", lambda *a, **k: monkeypatch_items.pop(0))
    monkeypatch.setattr(p, "_viewport_size", lambda *a, **k: 200)
    monkeypatch.setattr(p, "mm_per_pixel", lambda *a, **k: 0.28)
    monkeypatch.setitem(sys.modules, "slicer", types.SimpleNamespace(marker="fake"))

    result = measure()
    assert result["pixel_size_mm"]["XY"] == pytest.approx(0.28)


def test_version_marker_is_present_and_matches_the_output():
    """Every output carries the tool version.

    Slicer caches imported modules in sys.modules. Re-running the same lines
    in an already-open Slicer can return the old module, byte for byte, which
    looks exactly like "the fix did not work". The version marker makes the
    answer visible without guessing.
    """
    assert p.VERSION
    assert p.VERSION in p.module_report()


def test_reload_helper_drops_the_cached_module(monkeypatch):
    """reload_module() must work even when the module was never imported."""
    monkeypatch.setitem(sys.modules, "print_slicer_constants", None)
    p.reload_module()  # must not raise


def test_bootstrap_recovers_from_a_stale_module_without_the_new_methods(monkeypatch):
    """The bootstrap must recover from a stale cached module.

    A stale cached module may lack reload_module(), so even the recovery
    command could fail with AttributeError. measure_now.run() must load
    fresh code using only names present in the stale module.
    """
    bootstrap = _bootstrap()
    stale = types.SimpleNamespace(
        VERSION="OLD",
        __file__=str(BOOTSTRAP_TARGET),
        module_report=lambda: "stale",
        measure=lambda: {"version": "OLD"},
    )
    monkeypatch.setitem(sys.modules, "print_slicer_constants", stale)

    fresh = bootstrap.run()

    assert fresh.VERSION == "2026-10-08.3"
    assert fresh is not stale


def test_axis_names_are_ras_in_order():
    assert AXIS_NAMES == ("R", "A", "S")


def test_slicer_view_names_map_to_the_right_project_labels():
    labels = dict((name, label) for name, label, _o in VIEWS)
    assert labels["Red"] == "XY"
    assert labels["Green"] == "XZ"
    assert labels["Yellow"] == "YZ"


def test_viewport_width_reads_a_pythonqt_attribute_not_a_call():
    """PythonQt exposes width as an int attribute, not a method.

    Calling it raises "TypeError: 'int' object is not callable".
    """
    view = types.SimpleNamespace(width=200)
    assert viewport_width(view) == 200


def test_viewport_width_supports_a_plain_callable_too():
    """Plain Qt bindings do expose width() as a method."""
    class Callable:
        def width(self):
            return 137

    assert viewport_width(Callable()) == 137


def test_viewport_width_falls_back_to_geometry_when_width_is_absent():
    class NoWidth:
        geometry = types.SimpleNamespace(width=lambda: 512)

    assert viewport_width(NoWidth()) == 512


def test_viewport_width_returns_none_when_nothing_is_available():
    assert viewport_width(types.SimpleNamespace()) is None


def test_viewport_width_treats_zero_width_as_unavailable():
    """A collapsed view reports width 0; that means 'unknown', not 0 mm/px.

    mm_per_pixel is where a non-positive width becomes an error, so that a
    bad measurement cannot be mistaken for a real one.
    """
    assert viewport_width(types.SimpleNamespace(width=0)) is None
    with pytest.raises(ValueError, match="viewport"):
        mm_per_pixel(field_of_view_mm=250.0, viewport_px=0)


def test_view_labels_reports_the_two_shown_axes():
    assert view_labels("XY") == ("R", "A")
    assert view_labels("XZ") == ("R", "S")
    assert view_labels("YZ") == ("A", "S")


def test_view_labels_rejects_an_unknown_view():
    with pytest.raises(KeyError):
        view_labels("ZZ")


def test_view_labels_rejects_an_axis_used_twice():
    broken = dict(SHOWN_AXES)
    broken["XX"] = ("R", "R")
    with pytest.raises(ValueError, match="permutation"):
        view_labels("XX", shown=broken, depth={"XX": "A"})


def test_derived_axis_map_rejects_a_repeated_axis():
    with pytest.raises(ValueError, match="permutation"):
        p.derive_axis_map(("R", 1), ("R", 1), ("S", 1))


def test_slicer_module_is_imported_not_assumed(monkeypatch):
    """Slicer's console has `slicer` in the CONSOLE's globals, not this
    module's globals, so a globals() check fails even inside Slicer."""
    fake = types.SimpleNamespace(marker="fake-slicer")
    monkeypatch.setitem(sys.modules, "slicer", fake)
    assert p.slicer_module() is fake


def test_measure_outside_slicer_gives_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "slicer", None)
    with pytest.raises(NotSlicerConsole, match="Slicer"):
        measure()


def test_error_message_does_not_rely_on_the_python_version_banner(monkeypatch):
    """Slicer's console and a terminal print the SAME version banner."""
    monkeypatch.setitem(sys.modules, "slicer", None)
    with pytest.raises(NotSlicerConsole) as caught:
        measure()
    assert "Python Console" in str(caught.value)