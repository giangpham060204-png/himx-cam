"""Slicer-facing logic for the Targeting module.

No GUI code lives here: the widget calls these methods and displays what
they return. This module is the only new place that touches Slicer, so
everything testable stays testable.
"""

from __future__ import annotations

import pathlib
import sys

import qt
import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic

# TargetingLib/logic.py -> TargetingLib -> Targeting -> SlicerTargeting -> project root
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import slicer_bridge.live_frame as bridge  # noqa: E402
from targeting.camera_log import FILE_PREFIX, read_camera_targets  # noqa: E402
from targeting.converter import (  # noqa: E402
    OutOfFieldError,
    check_in_field_ras,
    convert,
    cube_to_ras,
)
from targeting.logbook import append_target  # noqa: E402

LOGS_DIR = PROJECT_ROOT / "logs"
TIP_NODE_NAME = "VirtualTip"
TARGETS_NODE_NAME = "Targets"
EXTERNAL_NODE_NAME = "ExternalTargets"
EXTERNAL_COLOUR = (1.0, 0.55, 0.1)
VIEW_WIDGETS = {"XY": "Red", "XZ": "Green", "YZ": "Yellow"}


def _qt_constant(*path):
    """Find a Qt enum across bindings (PyQt5/6 and PySide2/6 differ).

    Example: ("QEvent", "MouseButtonPress") finds qt.QEvent.MouseButtonPress
    or qt.QEvent.Type.MouseButtonPress. Returns None when absent, so a
    missing constant shows up as an inert filter instead of a crash.
    """
    target = qt
    for name in path:
        target = getattr(target, name, None)
        if target is None:
            return None
    return target


_MOUSE_PRESS = _qt_constant("QEvent", "MouseButtonPress") or _qt_constant(
    "QEvent", "Type", "MouseButtonPress"
)
_LEFT_BUTTON = _qt_constant("Qt", "LeftButton") or _qt_constant(
    "Qt", "MouseButton", "LeftButton"
)


def event_xy(event):
    """Widget-local pixel of a mouse event, across Qt5 (pos) and Qt6 (position)."""
    if hasattr(event, "position"):
        point = event.position().toPoint()
    else:
        point = event.pos()
    return float(point.x()), float(point.y())


class SliceClickFilter(qt.QObject):
    """Turn left-clicks on one slice view into callbacks while enabled.

    Returning True from eventFilter consumes the event, so Slicer's own
    controls never see it while picking. Disabled, everything passes
    through untouched.
    """

    def __init__(self, view, callback, parent=None):
        super().__init__(parent)
        self.view = view
        self.callback = callback
        self.enabled = False

    def eventFilter(self, obj, event):
        if not self.enabled:
            return False
        if event.type() == _MOUSE_PRESS and event.button() == _LEFT_BUTTON:
            x, y = event_xy(event)
            self.callback(self.view, x, y)
            return True
        return False


class TargetingLogic(ScriptedLoadableModuleLogic):
    """Turns a clicked pixel plus the view's depth into a placed target."""

    def __init__(self):
        super().__init__()
        self.placed = 0
        self.rejected = 0
        self.last_log_path = None

    # -- nodes --------------------------------------------------------

    def _display(self, node, color):
        if not node.GetDisplayNode():
            node.CreateDefaultDisplayNodes()
        display = node.GetDisplayNode()
        display.SetVisibility(True)
        display.SetColor(*color)
        return display

    def targets_node(self):
        node = slicer.mrmlScene.GetFirstNodeByName(TARGETS_NODE_NAME)
        if node is not None and not node.IsA("vtkMRMLMarkupsFiducialNode"):
            # A same-named node of the wrong class would shadow the real
            # list and spawn a duplicate on every click.
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsFiducialNode", TARGETS_NODE_NAME
            )
        display = self._display(node, (0.2, 0.4, 1.0))
        display.SetPointLabelsVisibility(True)  # current setter (not ...Visible)
        display.SetGlyphScale(4.0)
        return node

    def move_tip(self, ras):
        import vtk  # noqa: PLC0415

        sphere = vtk.vtkSphereSource()
        sphere.SetRadius(3.0)
        sphere.SetThetaResolution(24)
        sphere.SetPhiResolution(24)
        sphere.SetCenter(ras[0], ras[1], ras[2])
        sphere.Update()

        node = slicer.mrmlScene.GetFirstNodeByName(TIP_NODE_NAME)
        if node is not None and not node.IsA("vtkMRMLModelNode"):
            # Same reasoning as targets_node(): never operate on a
            # wrong-typed node that merely shares the name.
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", TIP_NODE_NAME)
        self._display(node, (0.15, 0.75, 0.35))
        node.GetDisplayNode().SetVisibility2D(True)
        node.SetAndObservePolyData(sphere.GetOutput())
        return node

    # -- the click pipeline ---------------------------------------------

    def place_from_click(self, view, u_px, v_px):
        """Place a target for a clicked pixel. Returns a result dict.

        "rejected" means the point is outside the field: nothing is placed.
        A failing log write still places the target and reports log_error.
        Unexpected errors are left to raise; the widget catches them.
        """
        slice_widget = slicer.app.layoutManager().sliceWidget(VIEW_WIDGETS[view])
        slice_node = slice_widget.sliceLogic().GetSliceNode()
        depth_mm = float(slice_node.GetSliceOffset())

        frame = bridge.live_frame()
        try:
            cube = convert(view, u_px, v_px, depth_mm, frame)
        except OutOfFieldError as exc:
            self.rejected += 1
            return {"status": "rejected", "message": str(exc)}
        ras = cube_to_ras(cube, frame)

        node = self.targets_node()
        index = node.AddControlPoint(ras[0], ras[1], ras[2])
        try:
            node.SetNthControlPointLabel(index, f"T{self.placed + 1}")
            self.move_tip(ras)
        except Exception:
            # Keep scene and counters consistent: a half-placed target must
            # not survive as an orphan the operator cannot explain.
            node.RemoveNthControlPoint(index)
            raise

        outcome = {
            "status": "placed",
            "view": view,
            "u_px": u_px,
            "v_px": v_px,
            "depth_mm": depth_mm,
            "cube": cube,
            "ras": ras,
            "log_error": None,
        }
        try:
            self.last_log_path = append_target(
                LOGS_DIR,
                view=view,
                u_px=u_px,
                v_px=v_px,
                depth_mm=depth_mm,
                cube=cube,
                ras=ras,
            )
        except OSError as exc:
            outcome["log_error"] = str(exc)

        self.placed += 1
        return outcome

    def external_targets_node(self):
        """The node holding camera-derived points. Rebuilt on every load.

        Kept apart from the clicked targets so that a glance tells you which is
        which, and rebuilt rather than appended to, so pressing the button twice
        cannot duplicate anything.
        """
        node = slicer.mrmlScene.GetFirstNodeByName(EXTERNAL_NODE_NAME)
        if node is not None:
            slicer.mrmlScene.RemoveNode(node)
        node = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLMarkupsFiducialNode", EXTERNAL_NODE_NAME
        )
        display = self._display(node, EXTERNAL_COLOUR)
        display.SetPointLabelsVisibility(True)
        display.SetGlyphScale(4.0)
        return node

    def load_external_targets(self, logs_dir=None):
        """Show every camera-derived target in the scene. Returns a summary.

        Rows that cannot be read, or that fall outside the field, are counted and
        named rather than dropped in silence. The field rule used here is the
        same one the click path uses, and it is checked against the live frame --
        so a row written while the field was elsewhere is refused, with the axis
        named.
        """
        logs_dir = LOGS_DIR if logs_dir is None else pathlib.Path(logs_dir)
        problems, good = [], []
        frame = bridge.live_frame()
        for path in sorted(logs_dir.glob(f"{FILE_PREFIX}*.csv")):
            log = read_camera_targets(path)
            problems.extend(log.problems)
            for target in log.targets:
                ras = dict(zip(("R", "A", "S"), target.ras))
                try:
                    check_in_field_ras(ras, frame)
                except OutOfFieldError as exc:
                    problems.append(f"{path.name}: {exc}")
                    continue
                good.append(target.ras)

        if not good:
            return {"status": "nothing", "loaded": 0, "skipped": len(problems),
                    "problems": tuple(problems), "node_name": EXTERNAL_NODE_NAME}

        node = self.external_targets_node()
        for index, ras in enumerate(good, start=1):
            at = node.AddControlPoint(ras[0], ras[1], ras[2])
            try:
                node.SetNthControlPointLabel(at, f"C{index}")
            except Exception:
                node.RemoveNthControlPoint(at)
                raise
        self.move_tip(good[-1])
        return {"status": "loaded", "loaded": len(good), "skipped": len(problems),
                "problems": tuple(problems), "node_name": EXTERNAL_NODE_NAME}

    def clear_targets(self):
        node = slicer.mrmlScene.GetFirstNodeByName(TARGETS_NODE_NAME)
        if node is not None:
            node.RemoveAllControlPoints()
        tip = slicer.mrmlScene.GetFirstNodeByName(TIP_NODE_NAME)
        if tip is not None:
            slicer.mrmlScene.RemoveNode(tip)
        self.placed = 0
        self.rejected = 0
