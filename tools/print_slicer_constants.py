"""Measure the frame constants. Run from Slicer's Python Console.

Legacy utility: normal operation reads the pixel->RAS mapping live via
slicer_bridge/live_frame.py, and tools/check_live_mapping.py verifies it
against the Data Probe. This script remains for inspecting the raw matrices.

Slicer knows two things that cannot be guessed reliably:
  1. which RAS axis each on-screen direction actually means
  2. how many millimetres one screen pixel covers

Guessing these is the single most likely source of a silent error, because a
wrong sign still produces plausible numbers. So measure, paste, done.

Two pitfalls this tool works around:

  * GetXYToRAS() maps XY pixels to RAS, so COLUMN j is the RAS direction of
    XY axis j: column 0 = screen-right, column 1 = screen-up, column 2 =
    slice normal. Reading ROWS looks right for Axial and Coronal, whose
    matrices are diagonal, and returns garbage for Sagittal, whose matrix is
    not.

  * Millimetres per pixel is field_of_view / viewport pixels. Using the
    volume's IJK dimensions instead gives a plausible wrong number.

Run from Slicer's Python Console:

    import sys; sys.path.insert(0, '/home/gianghp/projects/himx-cam/tools')
    import print_slicer_constants as p
    import json; print(json.dumps(p.measure(), indent=2))
"""

import json

AXIS_NAMES = ("R", "A", "S")

# Bump this whenever the measurement logic changes. It is printed with every
# run, because Slicer caches imported modules in sys.modules: re-running the
# same lines in an already-open Slicer can silently return the old module.
VERSION = "2026-10-08.3"

# (slicer view name, our view label, the orientation that view usually has)
VIEWS = (
    ("Red", "XY", "Axial"),
    ("Green", "XZ", "Coronal"),
    ("Yellow", "YZ", "Sagittal"),
)

# The usual layout, kept only to generate review notes. Never enforced.
SHOWN_AXES = {"XY": ("R", "A"), "XZ": ("R", "S"), "YZ": ("A", "S")}


class NotSlicerConsole(RuntimeError):
    """Raised when the slicer module cannot be imported."""


class Matrix3x4:
    """Minimal stand-in for vtkMatrix4x4, so tests need no Slicer."""

    def __init__(self, rows):
        self._rows = rows

    def GetElement(self, row, col):
        return self._rows[row][col]


def slicer_module():
    """Import and return Slicer's Python module.

    This must IMPORT slicer, not look for it in globals(). Slicer's Python
    console puts `slicer` in the console's own namespace, which is not this
    module's namespace, so a globals() check fails even inside Slicer.
    """
    try:
        import slicer
    except ImportError as exc:
        raise NotSlicerConsole(
            "Could not import the 'slicer' module, so this cannot measure "
            "anything yet.\n"
            "  Run it from Slicer: View > Python Console, or the Python icon "
            "in the toolbar.\n"
            "  From a terminal, run it as:\n"
            "    Slicer --no-splash --no-main-window --python-script "
            "print_slicer_constants.py\n"
            "  Note the Python version banner is identical in both places, so "
            "it cannot tell you which one you are in."
        ) from exc
    return slicer


def column_axis(matrix, column):
    """Return (axis letter, sign) for one COLUMN of a GetXYToRAS matrix.

    The matrix maps IJK to RAS, so column 0 is the RAS direction of IJK axis 0
    (screen-right), column 1 of IJK axis 1 (screen-up), and column 2 of IJK
    axis 2 (the slice normal).
    """
    values = [matrix.GetElement(row, column) for row in range(3)]
    index = max(range(3), key=lambda i: abs(values[i]))
    return AXIS_NAMES[index], (1 if values[index] >= 0 else -1)


def slice_normal(matrix):
    """Return (axis letter, sign) of the axis the slice cuts across."""
    return column_axis(matrix, 2)


def derive_axis_map(measured_normal, u, v):
    """Build a view's axis map, using the measured slice normal as depth."""
    binding = {"u": [u[0], u[1]], "v": [v[0], v[1]], "depth": [measured_normal[0], 1]}
    if sorted(axis for axis, _s in binding.values()) != ["A", "R", "S"]:
        raise ValueError(
            f"u={u} v={v} normal={measured_normal} is not a permutation of R,A,S"
        )
    return binding


def mm_per_pixel(field_of_view_mm, viewport_px):
    """Millimetres covered by one screen pixel."""
    if viewport_px <= 0:
        raise ValueError(f"viewport_px must be positive, got {viewport_px}")
    return field_of_view_mm / viewport_px


def view_labels(view, shown=None, depth=None):
    """Return (u_axis, v_axis) for a view label, or raise if it is not valid."""
    shown = SHOWN_AXES if shown is None else shown
    depth = {"XY": "S", "XZ": "A", "YZ": "R"} if depth is None else depth
    u_axis, v_axis = shown[view]
    depth_axis = depth[view]
    if sorted((u_axis, v_axis, depth_axis)) != ["A", "R", "S"]:
        raise ValueError(
            f"view {view}: {u_axis},{v_axis},{depth_axis} is not a permutation of R,A,S"
        )
    return u_axis, v_axis


def reload_module():
    """Re-read this file from disk, discarding Slicer's cached copy.

    Use this whenever you have edited the file while Slicer is open:
        import print_slicer_constants as p
        p.reload_module()
    """
    import importlib
    import os
    import sys

    path = globals().get("__file__")
    if path and os.path.exists(path):
        directory = os.path.dirname(path)
        for name in os.listdir(directory):
            if name.startswith("print_slicer_constants") and name.endswith(".pyc"):
                os.remove(os.path.join(directory, name))
        cache = os.path.join(directory, "__pycache__")
        if os.path.isdir(cache):
            for name in os.listdir(cache):
                if name.startswith("print_slicer_constants"):
                    os.remove(os.path.join(cache, name))

    sys.modules.pop("print_slicer_constants", None)
    return importlib.import_module("print_slicer_constants")


def module_report():
    """One line proving which version of this file is actually loaded."""
    return f"print_slicer_constants VERSION={VERSION} file={__file__}"


def viewport_width(view):
    """Width in pixels of a slice view widget.

    PythonQt, which Slicer uses, exposes QWidget.width as an already-evaluated
    int attribute, while plain PyQt/PySide expose it as a method; calling the
    int raises TypeError. Try the attribute first, then the method.

    Returns None if no width can be found at all.
    """
    width = getattr(view, "width", None)
    if callable(width):
        width = width()
    if isinstance(width, int):
        return width or None

    geometry = getattr(view, "geometry", None)
    if geometry is not None:
        value = getattr(geometry, "width", None)
        if callable(value):
            value = value()
        if isinstance(value, int):
            return value or None

    return None


def _viewport_size(view_name):
    """Width in pixels of a live view, or None if there is no GUI."""
    layout_manager = slicer_module().app.layoutManager()
    if layout_manager is None:
        return None
    widget = layout_manager.sliceWidget(view_name)
    if widget is None:
        return None
    return viewport_width(widget.sliceView())


def _slice_node(view_name, orientation):
    """Return the slice node for a view, with the orientation that view must have."""
    slicer = slicer_module()
    layout_manager = slicer.app.layoutManager()
    if layout_manager is not None:
        widget = layout_manager.sliceWidget(view_name)
        if widget is not None:
            return widget.sliceLogic().GetSliceNode()

    # Headless: no layout manager. Slicer's default Red/Green/Yellow slice nodes
    # all use Axial orientation, so build our own.
    node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSliceNode", f"{view_name}_{orientation}")
    node.SetOrientation(orientation)
    node.SetFieldOfView(100.0, 100.0, 1.0)
    return node


def measure():
    """Read the axis map and pixel sizes Slicer reports. Never asserts a layout."""
    slicer_module()
    out = {
        "version": VERSION,
        "pixel_size_mm": {},
        "view_axis_map": {},
        "normal": {},
        "notes": [],
    }

    for slicer_name, view, orientation in VIEWS:
        node = _slice_node(slicer_name, orientation)
        matrix = node.GetXYToRAS()

        normal = slice_normal(matrix)
        u = column_axis(matrix, 0)
        v = column_axis(matrix, 1)

        out["view_axis_map"][view] = derive_axis_map(normal, u, v)
        out["normal"][view] = [normal[0], normal[1]]

        expected = SHOWN_AXES[view]
        if (u[0], v[0]) != expected:
            out["notes"].append(
                f"{slicer_name} ({view}, {orientation}): measured u={u[0]} v={v[0]}, "
                f"usual layout is u={expected[0]} v={expected[1]}. Reported as "
                "measured. Check the orientation letters in the view corner."
            )

        field_of_view = node.GetFieldOfView()
        viewport_px = _viewport_size(slicer_name)
        if viewport_px is None:
            out["pixel_size_mm"][view] = None
            out["notes"].append(
                f"{slicer_name}: no viewport size available (no GUI). "
                "pixel_size_mm left null; open the Four-Up view and re-run."
            )
        else:
            out["pixel_size_mm"][view] = round(
                mm_per_pixel(field_of_view[0], viewport_px), 6
            )

    return out


try:
    _ = slicer_module()
except NotSlicerConsole:
    if __name__ == "__main__":
        raise
else:
    print(module_report())
    print(json.dumps(measure(), indent=2))
    print("Merge pixel_size_mm and view_axis_map into frame.json.")