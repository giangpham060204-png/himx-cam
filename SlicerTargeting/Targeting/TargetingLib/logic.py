"""Slicer-facing logic for the Targeting module.

No GUI code lives here: the widget calls these methods and displays what
they return. This module is the only new place that touches Slicer, so
everything testable stays testable.
"""

from __future__ import annotations

import json
import math
import pathlib
import shutil
import sys
import time
import traceback
from datetime import datetime

import qt
import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic

# TargetingLib/logic.py -> TargetingLib -> Targeting -> SlicerTargeting -> project root
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import slicer_bridge.live_frame as bridge  # noqa: E402
from targeting.camera_log import (  # noqa: E402
    FILE_PREFIX,
    MAX_RESIDUAL_PX,
    read_camera_targets,
)
from targeting.clicks import CLICKS_FILE  # noqa: E402
from targeting.converter import (  # noqa: E402
    OutOfFieldError,
    check_in_field_ras,
    convert,
    cube_to_ras,
    ras_to_cube,
)
from targeting.logbook import append_target  # noqa: E402
from targeting.probe import PROBE_FILE  # noqa: E402
from targeting.rig import load_rig  # noqa: E402
from targeting.triangulation import project, ray_for  # noqa: E402
from targeting.ray_guide import epipolar_segment, ray_segment  # noqa: E402
from targeting.video_launcher import (  # noqa: E402
    LauncherError,
    VideoProcess,
    build_command,
)

LOGS_DIR = PROJECT_ROOT / "logs"
TIP_NODE_NAME = "VirtualTip"
TIP_COLOUR = (0.0, 0.85, 1.0)
TARGETS_NODE_NAME = "Targets"
EXTERNAL_NODE_NAME = "ExternalTargets"
EXTERNAL_COLOUR = TIP_COLOUR
VIEW_WIDGETS = {"XY": "Red", "XZ": "Green", "YZ": "Yellow"}
PROBE_NODE_NAME = "SandboxProbe"
PROBE_COLOUR = (0.85, 0.1, 0.85)
PROBE_WRITE_INTERVAL_MS = 150
CAMERA_VIEWS = ("Red", "Green", "Yellow")
FRAMES_FOLDER = LOGS_DIR / "camera-frames"
STREAM_CONNECTOR_NAME = "CameraStreamConnector"
STREAM_PORT = 18944
STREAM_DEVICE_NAMES = ("c1", "c2", "c3")
STREAM_BIND_INTERVAL_MS = 200
STREAM_FOLDER = LOGS_DIR / "camera-stream"
STREAM_STATE_FILE = STREAM_FOLDER / "stream-state.json"
STREAM_COMMAND_FILE = STREAM_FOLDER / "stream-command.json"
STREAM_RIG_FILE = STREAM_FOLDER / "camera-rig.json"
STREAM_TRUTH_FILE = STREAM_FOLDER / "truth.json"
STREAM_RESULT_FILE = STREAM_FOLDER / "result.json"
VIDEO_LOG_FILE = STREAM_FOLDER / "video-sender.log"
STREAM_RAY_NODE_NAMES = ("CameraStreamRayC1", "CameraStreamRayC2",
                         "CameraStreamRayC3")
CAMERA_GUIDE_RAY_NODE_NAMES = (
    "CameraStreamClickRayC1",
    "CameraStreamClickRayC2",
    "CameraStreamClickRayC3",
)
# One epipolar line per (source click, target picture): each current click is
# projected into each of the other two camera views. Names are explicit about
# both ends so a line cannot be mistaken for the clicked view's own ray.
CAMERA_GUIDE_EPIPOLAR_NODE_NAMES = tuple(
    f"CameraStreamEpipolarC{target}FromC{source}"
    for source in (1, 2, 3)
    for target in (1, 2, 3)
    if source != target
)
# Names used by the pre-all-rays guide; cleared if an old scene still has them.
CAMERA_GUIDE_LEGACY_NODE_NAMES = (
    "CameraStreamClickRay",
    "CameraStreamEpipolarC1",
    "CameraStreamEpipolarC2",
    "CameraStreamEpipolarC3",
)
CAMERA_GUIDE_LENGTH_MM = 1000.0


def _camera_guide_line_name(source_index, target_index):
    """The epipolar-line node for one source click in one target picture."""
    return (f"CameraStreamEpipolarC{target_index + 1}"
            f"FromC{source_index + 1}")
STREAM_FRUSTUM_NODE_NAMES = ("CameraStreamFrustumC1", "CameraStreamFrustumC2",
                             "CameraStreamFrustumC3")
# A labelled point at each camera centre: the frustum alone is only an
# orientation hint and can be outside the 3D viewport when the field is in
# focus. These markers are the persistent "where is the camera" anchors.
STREAM_ORIGIN_NODE_NAMES = ("CameraStreamOriginC1", "CameraStreamOriginC2",
                            "CameraStreamOriginC3")
STREAM_PREDICTED_NODE_NAMES = ("CameraStreamPredictedC1",
                               "CameraStreamPredictedC2",
                               "CameraStreamPredictedC3")
CAMERA_CLICKS_NODE_NAME = "SandboxCameraClicks"
# One single-mark node per camera click: a markups node is visible in EVERY
# view showing its RAS region, and the three camera pictures overlap in RAS,
# so a shared node stacked all three marks in every pane.
# CAMERA_CLICKS_NODE_NAME is the pre-split name, kept only so a mark or a
# clear can remove the legacy node, never reuse it; the live marks live in
# CAMERA_CLICKS_NODE_NAMES, in CAMERA_VIEWS order.
CAMERA_CLICKS_NODE_NAMES = (
    "SandboxCameraClicksC1",
    "SandboxCameraClicksC2",
    "SandboxCameraClicksC3",
)
CAMERA_CLICK_LABELS = ("c1", "c2", "c3")
# Sandbox pane colours (tab:red / tab:green / tab:orange), one per camera.
CAMERA_CLICK_COLOURS = (
    (0.839, 0.153, 0.157),
    (0.173, 0.627, 0.173),
    (1.0, 0.498, 0.055),
)
CAMERA_CLICK_GLYPH_SCALE = 3.0
CAMERA_CLICKS_WRITE_INTERVAL_MS = 150
# The file the sandbox publishes beside the frame PNGs: one projection matrix
# per camera (rig 3D mm -> image pixel). Spawn reads it; nothing here recomputes
# rig optics -- the file is the single source.
RIG_FILE = "camera-rig.json"
# Pre-C2 spawn marks lived in these nodes; a spawn now seeds the camera clicks
# instead, so these names are only ever removed, never reused.
_LEGACY_SPAWN_NODE_NAMES = (
    "SandboxSpawnedPoint",
    "SandboxSpawnedPointC1",
    "SandboxSpawnedPointC2",
    "SandboxSpawnedPointC3",
)


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
_MOUSE_MOVE = _qt_constant("QEvent", "MouseMove") or _qt_constant(
    "QEvent", "Type", "MouseMove"
)
_MOUSE_RELEASE = _qt_constant("QEvent", "MouseButtonRelease") or _qt_constant(
    "QEvent", "Type", "MouseButtonRelease"
)
_WHEEL_EVENT = _qt_constant("QEvent", "Wheel") or _qt_constant(
    "QEvent", "Type", "Wheel"
)
_LEFT_BUTTON = _qt_constant("Qt", "LeftButton") or _qt_constant(
    "Qt", "MouseButton", "LeftButton"
)
_CONTROL_MODIFIER = _qt_constant("Qt", "ControlModifier") or _qt_constant(
    "Qt", "KeyboardModifier", "ControlModifier"
)
_SHIFT_MODIFIER = _qt_constant("Qt", "ShiftModifier") or _qt_constant(
    "Qt", "KeyboardModifier", "ShiftModifier"
)
_ALT_MODIFIER = _qt_constant("Qt", "AltModifier") or _qt_constant(
    "Qt", "KeyboardModifier", "AltModifier"
)


def event_xy(event):
    """Widget-local pixel of a mouse event, across Qt5 (pos) and Qt6 (position)."""
    if hasattr(event, "position"):
        point = event.position().toPoint()
    else:
        point = event.pos()
    return float(point.x()), float(point.y())


class SliceClickFilter(qt.QObject):
    """Place a camera click on a left press without taking the gesture.

    The callback runs on the press and moves that camera's mark immediately.
    The press is then allowed to continue to the slice view, so a press on
    the mark still starts a normal markups drag: the mark is placed at the
    press, and dragging continues from there. Consuming the press (or its
    moves) would break that drag and also stop the view repainting while the
    button is down.

    Ctrl+left is different: it pans the view and never places a camera click.
    Shift+left and Alt+left also skip the click so Slicer's own pan/zoom
    gestures keep working. This is the escape hatch for inspecting a zoomed
    camera picture without leaving marks behind.

    The filter must be installed on the widget that receives the press (the
    ctkVTKOpenGLNativeWidget child of qMRMLSliceView). Installed on the
    parent, Qt delivers the press to the child first and the callback runs
    only during propagation, after the drag has already started. Disabled,
    everything passes through.
    """

    def __init__(self, view, callback, parent=None):
        super().__init__(parent)
        self.view = view
        self.callback = callback
        self.enabled = True
        self.click_enabled = False
        self._panning = False
        self._pan_last = None

    def eventFilter(self, obj, event):
        if not self.enabled:
            return False
        event_type = event.type()
        modifiers = event.modifiers() if hasattr(event, "modifiers") else 0

        if event_type == _WHEEL_EVENT:
            # A wheel is a zoom, never a continuation of a pan. Clearing the
            # state also recovers if a release was lost outside the widget.
            self._panning = False
            self._pan_last = None
            if _CONTROL_MODIFIER and (modifiers & _CONTROL_MODIFIER):
                angle = 0
                if hasattr(event, "angleDelta"):
                    angle = event.angleDelta().y()
                elif hasattr(event, "delta"):
                    angle = event.delta()
                if angle:
                    self._zoom_view(angle)
                    return True
            return False

        if self._panning:
            if event_type == _MOUSE_MOVE:
                buttons = (event.buttons() if hasattr(event, "buttons")
                           else _LEFT_BUTTON)
                if _LEFT_BUTTON and not (buttons & _LEFT_BUTTON):
                    self._panning = False
                    self._pan_last = None
                    return False
                x, y = event_xy(event)
                last = self._pan_last or (x, y)
                self._pan_last = (x, y)
                self._pan_view(x - last[0], y - last[1])
                return True
            if event_type == _MOUSE_RELEASE:
                self._panning = False
                self._pan_last = None
                return False

        if event_type == _MOUSE_PRESS and event.button() == _LEFT_BUTTON:
            if _CONTROL_MODIFIER and (modifiers & _CONTROL_MODIFIER):
                # Record the drag but let the press reach the view: Qt then
                # establishes its mouse grab and the following moves arrive
                # here. Consuming the press made real Ctrl+drag inert.
                self._panning = True
                self._pan_last = event_xy(event)
                return False
            if ((_SHIFT_MODIFIER and (modifiers & _SHIFT_MODIFIER))
                    or (_ALT_MODIFIER and (modifiers & _ALT_MODIFIER))):
                # Let Slicer's native pan/zoom gestures run, without a click.
                return False
            if self.click_enabled:
                x, y = event_xy(event)
                self.callback(self.view, x, y)
        return False

    def _zoom_view(self, angle):
        """Ctrl+wheel zoom around the view centre, not the pointer.

        Slicer's native wheel zoom is cursor-anchored; repeated steps with a
        moving pointer walk the picture off-centre. The camera views are for
        inspection, so keep their centre fixed.
        """
        try:
            slice_widget = slicer.app.layoutManager().sliceWidget(
                VIEW_WIDGETS[self.view])
            if slice_widget is None:
                return
            node = slice_widget.sliceLogic().GetSliceNode()
            field = node.GetFieldOfView()
            factor = 1.1 ** (-float(angle) / 120.0)
            node.SetFieldOfView(
                max(1e-3, float(field[0]) * factor),
                max(1e-3, float(field[1]) * factor),
                float(field[2]))
            node.UpdateMatrices()
            node.Modified()
            slice_widget.sliceView().renderWindow().Render()
        except Exception:  # noqa: BLE001 - a zoom must not break the view
            traceback.print_exc()

    def _pan_view(self, dx, dy):
        """Move the slice view with the pointer; content follows the drag."""
        try:
            slice_widget = slicer.app.layoutManager().sliceWidget(
                VIEW_WIDGETS[self.view])
            if slice_widget is None:
                return
            node = slice_widget.sliceLogic().GetSliceNode()
            view = slice_widget.sliceView()
            dimensions = node.GetDimensions()
            scale_x = float(dimensions[0]) / max(float(view.width), 1.0)
            scale_y = float(dimensions[1]) / max(float(view.height), 1.0)
            dx_vtk = float(dx) * scale_x
            dy_vtk = -float(dy) * scale_y
            matrix = node.GetXYToRAS()
            ras_dx = (matrix.GetElement(0, 0) * dx_vtk
                      + matrix.GetElement(0, 1) * dy_vtk)
            ras_dy = (matrix.GetElement(1, 0) * dx_vtk
                      + matrix.GetElement(1, 1) * dy_vtk)
            ras_dz = (matrix.GetElement(2, 0) * dx_vtk
                      + matrix.GetElement(2, 1) * dy_vtk)
            slice_to_ras = node.GetSliceToRAS()
            for axis, delta in enumerate((ras_dx, ras_dy, ras_dz)):
                slice_to_ras.SetElement(
                    axis, 3, slice_to_ras.GetElement(axis, 3) - delta)
            node.UpdateMatrices()
            node.Modified()
            slice_widget.sliceView().renderWindow().Render()
        except Exception:  # noqa: BLE001 - a pan must not crash the view
            traceback.print_exc()


class ThreeDViewClickFilter(qt.QObject):
    """Turn Ctrl+left in the 3D view into a camera orbit.

    The press is allowed through so Qt establishes its normal mouse grab; the
    following moves are consumed and applied to the camera directly. This does
    not depend on the native interactor's Ctrl+left zoom state, and the module
    furniture is non-selectable so it cannot take the drag either.
    """

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self._dragging = False
        self._last = None

    def eventFilter(self, obj, event):
        event_type = event.type()
        modifiers = event.modifiers() if hasattr(event, "modifiers") else 0

        if event_type == _WHEEL_EVENT:
            self._dragging = False
            self._last = None
            return False

        if (event_type == _MOUSE_PRESS
                and event.button() == _LEFT_BUTTON
                and _CONTROL_MODIFIER
                and (modifiers & _CONTROL_MODIFIER)):
            # Do not consume: the native press keeps Qt's mouse grab alive.
            self._dragging = True
            self._last = event_xy(event)
            return False

        if self._dragging:
            if event_type == _MOUSE_MOVE:
                buttons = (event.buttons() if hasattr(event, "buttons")
                           else _LEFT_BUTTON)
                if _LEFT_BUTTON and not (buttons & _LEFT_BUTTON):
                    self._dragging = False
                    self._last = None
                    return False
                x, y = event_xy(event)
                last = self._last or (x, y)
                self._last = (x, y)
                self._orbit(x - last[0], y - last[1])
                return True
            if event_type == _MOUSE_RELEASE:
                self._dragging = False
                self._last = None
                return False
        return False

    def _orbit(self, dx, dy):
        try:
            view = self.view
            if view is None:
                return
            renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
            if renderer is None:
                return
            camera = renderer.GetActiveCamera()
            if camera is None:
                return
            camera.Azimuth(-float(dx) * 0.5)
            camera.Elevation(float(dy) * 0.5)
            camera.OrthogonalizeViewUp()
            renderer.ResetCameraClippingRange()
            view.renderWindow().Render()
        except Exception:  # noqa: BLE001 - a drag must not break the view
            traceback.print_exc()


def _matrix4(vtk_matrix):
    """A vtkMatrix4x4 as nested tuples, the shape targeting.clicks reads."""
    return tuple(tuple(float(vtk_matrix.GetElement(row, column)) for column in range(4))
                 for row in range(4))


def _apply4(matrix, vector):
    """A 4x4 nested sequence applied to a 4-vector.

    Local twin of targeting.clicks' private apply: clicks.py is frozen by an
    earlier chunk, and this is the one line camera_pixel needs after
    _click_ras has resolved the RAS.
    """
    return tuple(sum(float(matrix[row][column]) * vector[column] for column in range(4))
                 for row in range(4))


def _det3(matrix):
    return (matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
            - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
            + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0]))


def _det4(matrix):
    """The determinant of a 4x4 nested sequence, for the singularity refusal."""
    total = 0.0
    for column in range(4):
        minor = [[float(matrix[row][other]) for other in range(4) if other != column]
                 for row in range(1, 4)]
        total += ((-1.0) ** column) * float(matrix[0][column]) * _det3(minor)
    return total


class TargetingLogic(ScriptedLoadableModuleLogic):
    """Turns a clicked pixel plus the view's depth into a placed target."""

    def __init__(self):
        super().__init__()
        self.placed = 0
        self.rejected = 0
        self.last_log_path = None
        self.probe_view = ""
        self.last_probe_path = None
        self.probe_error = None
        self.clicks = {}
        self.last_clicks_path = None
        self.click_outside = set()
        self.frames_folder = None
        self._clicks_timer = None
        self._clicks_updating = False
        self._clicks_observed_ids = set()
        self._stream_timer = None
        self._stream_bound = False
        self._stream_node_ids = ()
        self._stream_status = "off"
        self._stream_message = "camera stream: off"
        self._stream_views = {}
        self._stream_port = STREAM_PORT
        self._stream_session_start = 0.0
        self._stream_rig = None
        self._stream_truth = None
        self._stream_state = None
        self._stream_state_mtime = None
        self._stream_result = None
        self._stream_result_mtime = None
        self._stream_cameras_drawn = False
        self._stream_cameras_fitted = False
        self._markup_drag_active = False
        self._ray_guide_message = "ray guide: click a camera view"
        self._video = None

    # -- nodes --------------------------------------------------------

    def _colour_display(self, display, color):
        """Set a colour that survives Slicer's selected/active rendering.

        Slicer draws a selected markups node with its SelectedColor and the
        active point with its ActiveColor. Those defaults are red/salmon, so a
        node painted only with SetColor loses its red/green/orange/cyan coding
        the moment Slicer selects it. Setting all three to the intended colour
        keeps the camera rays and the measured point readable.
        """
        wanted = tuple(float(value) for value in color)
        pairs = (("GetColor", "SetColor"),
                 ("GetSelectedColor", "SetSelectedColor"),
                 ("GetActiveColor", "SetActiveColor"))
        for getter_name, setter_name in pairs:
            apply = getattr(display, setter_name, None)
            if not callable(apply):
                continue
            read = getattr(display, getter_name, None)
            try:
                current = tuple(float(value) for value in read())
            except Exception:  # noqa: BLE001 - a colour read is best effort
                current = None
            if (current is None or len(current) != len(wanted)
                    or any(abs(a - b) > 1e-6 for a, b in zip(current, wanted))):
                apply(*wanted)

    def _display(self, node, color):
        if not node.GetDisplayNode():
            node.CreateDefaultDisplayNodes()
        display = node.GetDisplayNode()
        if not display.GetVisibility():
            display.SetVisibility(True)
        self._colour_display(display, color)
        return display

    @staticmethod
    def _unpickable(display):
        """Keep display furniture from stealing a 3D-view drag."""
        for name in ("SetPickable", "SetSelectable"):
            setter = getattr(display, name, None)
            if callable(setter):
                try:
                    setter(False)
                except Exception:  # noqa: BLE001 - display only
                    pass

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
        display = self._display(node, TIP_COLOUR)
        self._unpickable(display)
        display.SetVisibility2D(True)
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

    # -- the probe the sandbox follows ----------------------------------

    def probe_node(self):
        """The one point Slicer publishes for the camera sandbox.

        A markup point rather than a model, because it can be dragged in the
        slice and 3D views -- which is exactly the interaction the sandbox
        follows -- and because the point's position can be read back without
        guessing at geometry.
        """
        node = slicer.mrmlScene.GetFirstNodeByName(PROBE_NODE_NAME)
        if node is not None and not node.IsA("vtkMRMLMarkupsFiducialNode"):
            # Same reasoning as targets_node(): never operate on a wrong-typed
            # node that merely shares the name.
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsFiducialNode", PROBE_NODE_NAME
            )
            display = self._display(node, PROBE_COLOUR)
            display.SetPointLabelsVisibility(False)
            display.SetGlyphScale(4.0)
        return node

    def probe_ras(self):
        """Where the probe is, or None when there is no probe yet."""
        node = slicer.mrmlScene.GetFirstNodeByName(PROBE_NODE_NAME)
        if node is None or node.GetNumberOfControlPoints() < 1:
            return None
        position = [0.0, 0.0, 0.0]
        node.GetNthControlPointPosition(0, position)
        return tuple(float(value) for value in position)

    def move_probe(self, ras):
        """Put the probe at one RAS point, creating it on first use."""
        node = self.probe_node()
        if node.GetNumberOfControlPoints() == 0:
            node.AddControlPoint(ras[0], ras[1], ras[2])
        else:
            node.SetNthControlPointPosition(0, ras[0], ras[1], ras[2])
        return node

    def publish_probe(self, logs_dir=None):
        """Write where the probe is, atomically, for the sandbox to read.

        Atomic matters: the sandbox polls this file, and a reader that caught a
        half-written one would have to guess. Writing a temporary name and
        renaming over the old file means a reader sees either the old point or
        the new one, never a mixture.

        Returns the path, or None when there is no probe yet. A failure is
        reported by returning the error text, because this runs inside a Qt
        timer where an exception would only reach the console.
        """
        ras = self.probe_ras()
        if ras is None:
            self.probe_error = None
            return None
        try:
            frame = bridge.live_frame()
            cube = ras_to_cube(ras, frame)
            folder = LOGS_DIR if logs_dir is None else pathlib.Path(logs_dir)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / PROBE_FILE
            temporary = path.with_name(path.name + ".tmp")
            payload = {
                "cube": [float(value) for value in cube],
                "ras": [float(value) for value in ras],
                "view": self.probe_view,
                "when": datetime.now().isoformat(timespec="seconds"),
            }
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(path)
        except Exception as exc:              # noqa: BLE001 - reported, not raised
            traceback.print_exc()
            self.probe_error = str(exc)
            return None
        self.probe_error = None
        self.last_probe_path = path
        return path

    def watch_probe(self, interval_ms=PROBE_WRITE_INTERVAL_MS):
        """Publish the probe when a drag ends, or shortly after a typed edit.

        A drag fires hundreds of PointModifiedEvents. Doing module work on
        every one makes a plain Slicer drag stutter as soon as the module is
        loaded. The interaction events let us do nothing during the drag and
        publish exactly once on release; a modified event outside a drag
        (for example a typed coordinate) still gets the short debounce.
        """
        if getattr(self, "_probe_timer", None) is not None:
            return self._probe_timer
        self._probe_timer = qt.QTimer()
        self._probe_timer.setSingleShot(True)
        self._probe_timer.setInterval(int(interval_ms))
        self._probe_timer.timeout.connect(self.publish_probe)
        # The MRML classes live in Slicer's own namespace, not in vtk, and the
        # node instance carries the event id: ask it rather than guess a path
        # that differs between Slicer builds.
        node = self.probe_node()
        self._observe_drag_events(node, self._on_probe_point_modified,
                                  self.publish_probe)
        return self._probe_timer

    def _on_probe_point_modified(self):
        if self._markup_drag_active:
            return
        timer = getattr(self, "_probe_timer", None)
        if timer is not None:
            timer.start()

    def _observe_drag_events(self, node, on_modified, republish):
        """Observe drag start/end as well as direct coordinate changes.

        Start/end are what make the expensive tail -- file writes and guide
        regeneration -- wait until the mouse button is released. The modified
        observer remains for edits that have no mouse interaction (Data module
        table, script, typed coordinate).
        """
        node.AddObserver(node.PointModifiedEvent, lambda *_args: on_modified())
        start_event = getattr(node, "PointStartInteractionEvent", None)
        if start_event is not None:
            node.AddObserver(start_event, lambda *_args: self._begin_markup_drag())
        end_event = getattr(node, "PointEndInteractionEvent", None)
        if end_event is not None:
            node.AddObserver(
                end_event,
                lambda *_args, callback=republish: self._end_markup_drag(callback))

    def _begin_markup_drag(self):
        """Stop module timers so a drag does no module work.

        The stream is never touched here: it keeps playing throughout, as the
        operator requires.
        """
        if self._markup_drag_active or self._clicks_updating:
            return
        self._markup_drag_active = True
        for name in ("_probe_timer", "_clicks_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()

    def _end_markup_drag(self, republish=None):
        """Publish the final position once, on release."""
        self._markup_drag_active = False
        if republish is not None:
            try:
                republish()
            except Exception:  # noqa: BLE001 - a drag must not crash
                traceback.print_exc()

    def stop_probe_watch(self):
        """Stop only the probe timer; the camera stream keeps its own timer."""
        timer = getattr(self, "_probe_timer", None)
        if timer is not None:
            timer.stop()

    def stop_watch(self):
        for name in ("_probe_timer", "_clicks_timer", "_stream_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()

    def set_probe_from_click(self, view, u_px, v_px):
        """Move the probe to what a click in a slice view means. Nothing is logged.

        The same pixel-plus-depth mapping the click path uses, with one
        difference that matters: a probe is a question, not a measurement, so it
        never touches the target log.
        """
        slice_widget = slicer.app.layoutManager().sliceWidget(VIEW_WIDGETS[view])
        depth_mm = float(slice_widget.sliceLogic().GetSliceNode().GetSliceOffset())
        frame = bridge.live_frame()
        cube = convert(view, u_px, v_px, depth_mm, frame)
        ras = cube_to_ras(cube, frame)
        self.probe_view = view
        self.move_probe(ras)
        self.publish_probe()
        return {
            "status": "probed",
            "view": view,
            "u_px": u_px,
            "v_px": v_px,
            "depth_mm": depth_mm,
            "cube": cube,
            "ras": ras,
        }

    def external_targets_node(self, rebuild=True):
        """The node holding camera-derived points.

        Kept apart from the clicked targets so that a glance tells you which is
        which. A history load rebuilds it; a live result reuses it and replaces
        its single point, because a drag can solve many times per second.
        """
        node = slicer.mrmlScene.GetFirstNodeByName(EXTERNAL_NODE_NAME)
        if node is not None and (
                rebuild or not node.IsA("vtkMRMLMarkupsFiducialNode")):
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsFiducialNode", EXTERNAL_NODE_NAME
            )
        display = self._display(node, EXTERNAL_COLOUR)
        self._unpickable(display)
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
                if float(target.residual_px) > MAX_RESIDUAL_PX:
                    problems.append(
                        f"{path.name}: {target.timestamp}: residual "
                        f"{float(target.residual_px):.2f} px exceeds "
                        f"{MAX_RESIDUAL_PX:.2f} px")
                    continue
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

    def load_result_target(self, result):
        """Show only the current stream result's point in the 3D scene.

        The camera log keeps the measurement history, but a live drag can solve
        dozens of times. Loading that history on every tick piles up points and
        labels and makes the 3D view sluggish; the history remains available
        through the explicit Load external targets button.
        """
        ras = result.get("ras")
        if not isinstance(ras, (list, tuple)) or len(ras) != 3:
            return {"status": "nothing", "loaded": 0, "skipped": 1,
                    "problems": ("the result has no field point",),
                    "node_name": EXTERNAL_NODE_NAME}
        try:
            point = tuple(float(value) for value in ras)
        except (TypeError, ValueError):
            return {"status": "nothing", "loaded": 0, "skipped": 1,
                    "problems": ("the result point is not numeric",),
                    "node_name": EXTERNAL_NODE_NAME}
        if not all(math.isfinite(value) for value in point):
            return {"status": "nothing", "loaded": 0, "skipped": 1,
                    "problems": ("the result point is not finite",),
                    "node_name": EXTERNAL_NODE_NAME}

        node = self.external_targets_node(rebuild=False)
        if node.GetNumberOfControlPoints() == 1:
            node.SetNthControlPointPosition(0, point[0], point[1], point[2])
            at = 0
        else:
            node.RemoveAllControlPoints()
            at = node.AddControlPoint(point[0], point[1], point[2])
        node.SetNthControlPointLabel(at, "result")
        self.move_tip(point)
        return {"status": "loaded", "loaded": 1, "skipped": 0,
                "problems": (), "node_name": EXTERNAL_NODE_NAME}

    # -- the live camera stream ------------------------------------------

    def _stream_connector(self):
        """The one OpenIGTLinkIF server used by the camera stream."""
        node = slicer.mrmlScene.GetFirstNodeByName(STREAM_CONNECTOR_NAME)
        if node is not None and not node.IsA("vtkMRMLIGTLConnectorNode"):
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLIGTLConnectorNode", STREAM_CONNECTOR_NAME)
        return node

    def _stream_port_in_use(self):
        """True when something else already holds the stream port."""
        import socket  # noqa: PLC0415 - only needed on a failed listener

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", int(self._stream_port)))
        except OSError:
            return True
        finally:
            probe.close()
        return False

    def camera_stream_status(self):
        """One truthful line for the panel, plus the bound view names."""
        connector = slicer.mrmlScene.GetFirstNodeByName(STREAM_CONNECTOR_NAME)
        state = int(connector.GetState()) if connector is not None else 0
        connected_state = int(getattr(connector, "StateConnected", 2))
        off_state = int(getattr(connector, "StateOff", 0))
        stream_state = self._stream_state or {}
        if self._stream_status in ("waiting", "connected", "disconnected"):
            if state == connected_state:
                self._stream_status = "connected"
                frame = stream_state.get("frame_id")
                if stream_state.get("paused"):
                    self._stream_message = (
                        f"camera stream: paused at frame {frame} "
                        "\u2014 clear camera clicks to resume")
                elif frame is not None:
                    self._stream_message = f"camera stream: playing frame {frame}"
                    if not stream_state.get("calibrated", True):
                        self._stream_message += " \u00b7 uncalibrated (display only)"
                else:
                    self._stream_message = "camera stream: connected (c1, c2, c3)"
            elif state == off_state and self._stream_status == "waiting":
                self._stream_status = "error"
                if self._stream_port_in_use():
                    reason = (
                        f"port {self._stream_port} is already in use; close "
                        "the old Slicer or wait")
                else:
                    reason = (
                        f"the listener did not start on port "
                        f"{self._stream_port}; see the Slicer log")
                self._stream_message = (
                    f"camera stream: {reason}, then press Connect again")
            elif self._stream_status == "connected":
                self._stream_status = "disconnected"
                self._stream_message = (
                    "camera stream: disconnected (last frame shown)")
        return {
            "status": self._stream_status,
            "message": self._stream_message,
            "port": self._stream_port,
            "state": state,
            "frame_id": stream_state.get("frame_id"),
            "paused": bool(stream_state.get("paused")),
            "calibrated": stream_state.get("calibrated"),
            "views": dict(self._stream_views),
        }

    def _uv_executable(self):
        """Find the uv that owns the project environment."""
        uv = shutil.which("uv")
        if uv:
            return uv
        candidate = pathlib.Path.home() / ".local" / "bin" / "uv"
        if candidate.is_file():
            return str(candidate)
        raise LauncherError(
            "uv is not on PATH; install uv or run the sender manually")

    def start_video(self, video_files, calibration=None, truth=None, fps=8.0):
        """Start the external sender with three explicit clips."""
        command = build_command(
            video_files, calibration=calibration, truth=truth, fps=fps,
            port=self._stream_port, uv=self._uv_executable())
        if self._video is not None:
            self._video.stop()
        process = VideoProcess(
            command, cwd=PROJECT_ROOT, log_path=VIDEO_LOG_FILE)
        self._video = process
        return process.start()

    def stop_video(self):
        """Stop the external sender, if this module started one."""
        if self._video is None:
            return {
                "status": "off",
                "message": "video: off",
                "pid": None,
                "returncode": None,
                "log_path": str(VIDEO_LOG_FILE),
                "log_tail": "",
            }
        return self._video.stop()

    def video_status(self):
        """The sender's non-blocking status for the panel."""
        if self._video is None:
            return {
                "status": "off",
                "message": "video: off",
                "pid": None,
                "returncode": None,
                "log_path": str(VIDEO_LOG_FILE),
                "log_tail": "",
            }
        return self._video.poll()

    def connect_camera_stream(self, port=STREAM_PORT):
        """Start the OpenIGTLink server and bind c1/c2/c3 when they arrive.

        The sender may start before or after this call: the connector is a
        server, and a timer binds the three incoming volumes as soon as they
        exist. Nothing here decodes video; Slicer sees images only.
        """
        if not hasattr(slicer, "vtkMRMLIGTLConnectorNode"):
            self._stream_status = "unavailable"
            self._stream_message = (
                "camera stream: install the SlicerOpenIGTLink extension "
                "(OpenIGTLinkIF) first")
            return self.camera_stream_status()

        self._stream_port = int(port)
        connector = self._stream_connector()
        try:
            if connector.GetState() != connector.StateOff:
                connector.Stop()
            connector.SetTypeServer(self._stream_port)
            connector.Start()
        except Exception as exc:  # noqa: BLE001 - shown to the operator
            self._stream_status = "error"
            self._stream_message = (
                f"camera stream: could not listen on port {port}: {exc}")
            return self.camera_stream_status()

        self._stream_bound = False
        self._stream_node_ids = ()
        self._stream_views = {}
        self._stream_status = "waiting"
        self._stream_message = (
            f"camera stream: waiting for {', '.join(STREAM_DEVICE_NAMES)} "
            f"on port {self._stream_port}")
        self._stream_session_start = time.time()
        self._stream_rig = None
        self._stream_truth = None
        self._stream_state = None
        self._stream_state_mtime = None
        self._stream_result = None
        self._stream_result_mtime = None
        self._stream_cameras_drawn = False
        self._stream_cameras_fitted = False
        self._clear_stream_geometry()
        self._watch_camera_stream()
        self._bind_camera_stream_views()
        return self.camera_stream_status()

    def _watch_camera_stream(self, interval_ms=STREAM_BIND_INTERVAL_MS):
        """Bind incoming volumes without blocking Slicer's event loop."""
        if self._stream_timer is None:
            self._stream_timer = qt.QTimer()
            self._stream_timer.setInterval(int(interval_ms))
            self._stream_timer.timeout.connect(self._stream_tick)
        self._stream_timer.start()

    def _stream_tick(self):
        """Bind volumes, then pick up the frame state and a solved result."""
        self._bind_camera_stream_views()
        self._read_stream_state()
        self._ensure_stream_cameras()
        self._refresh_stream_result()

    def _ensure_stream_cameras(self):
        """Show the camera rig as soon as a usable calibration exists.

        The rig file can arrive after the three volumes; retrying here means
        camera positions are not held hostage to the first solved click (or
        to any click at all). A failed load is retried on the next tick.
        """
        if self._stream_cameras_drawn:
            return
        self._load_stream_metadata()
        if not (self._stream_rig or {}).get("cameras"):
            return
        try:
            self._draw_stream_frusta()
        except Exception:  # noqa: BLE001 - a timer must not raise
            traceback.print_exc()

    def _bind_camera_stream_views(self):
        """Show c1/c2/c3 in Red/Green/Yellow once all three have arrived."""
        nodes = []
        for name in STREAM_DEVICE_NAMES:
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            if (node is None or not node.IsA("vtkMRMLScalarVolumeNode")
                    or node.GetImageData() is None):
                if self._stream_status not in ("off", "unavailable", "error",
                                               "paused"):
                    self._stream_status = "waiting"
                    self._stream_message = (
                        f"camera stream: waiting for "
                        f"{', '.join(STREAM_DEVICE_NAMES)} on port "
                        f"{self._stream_port}")
                return self.camera_stream_status()
            nodes.append(node)

        ids = tuple(node.GetID() for node in nodes)
        if self._stream_bound and ids == self._stream_node_ids:
            return self.camera_stream_status()

        try:
            views = self._show_camera_views(nodes)
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            self._stream_status = "error"
            self._stream_message = (
                f"camera stream: could not show the views: {exc}")
            return self.camera_stream_status()

        self._stream_bound = True
        self._stream_node_ids = ids
        self._stream_views = views
        self._stream_status = "connected"
        self._stream_message = "camera stream: connected (c1, c2, c3)"
        self._load_stream_metadata()
        try:
            self._draw_stream_frusta()
        except Exception:  # noqa: BLE001 - the view must still bind
            traceback.print_exc()
        return self.camera_stream_status()

    def _load_stream_metadata(self):
        """Read the projection matrices and truth the sender publishes."""
        if self._stream_rig is None:
            try:
                self._stream_rig = json.loads(
                    STREAM_RIG_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._stream_rig = None
        if self._stream_truth is None:
            try:
                self._stream_truth = json.loads(
                    STREAM_TRUTH_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._stream_truth = None

    def _read_stream_state(self):
        """Read the frame/pause state the sender publishes with the stream."""
        try:
            stat = STREAM_STATE_FILE.stat()
        except OSError:
            return
        if stat.st_mtime < self._stream_session_start:
            return
        if self._stream_state_mtime == stat.st_mtime_ns:
            return
        try:
            payload = json.loads(STREAM_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._stream_state = payload
        self._stream_state_mtime = stat.st_mtime_ns

    def _refresh_stream_result(self):
        """Load a new solved click set, show its point, and draw the rays."""
        try:
            stat = STREAM_RESULT_FILE.stat()
        except OSError:
            return
        if stat.st_mtime < self._stream_session_start:
            return
        if self._stream_result_mtime == stat.st_mtime_ns:
            return
        try:
            result = json.loads(STREAM_RESULT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        previous_key = (self._result_key(self._stream_result)
                        if self._stream_result else None)
        key = self._result_key(result)
        self._stream_result = result
        self._stream_result_mtime = stat.st_mtime_ns
        self._load_stream_metadata()
        if key == previous_key:
            # The sender rewrites result.json every frame, but the geometry is
            # unchanged. Reapplying it would redraw the same overlays at 8 Hz.
            return
        if result.get("status") == "found":
            self._set_camera_ray_guide_visible(False)
            try:
                self.load_result_target(result)
            except Exception:  # noqa: BLE001 - a timer must not raise
                traceback.print_exc()
            try:
                self._draw_stream_frusta()
                self._draw_stream_rays(result)
                self._draw_stream_predicted(result)
            except Exception:  # noqa: BLE001 - a timer must not raise
                traceback.print_exc()
        else:
            # A refused click set must not leave the previous result's point
            # or rays on screen. Keep the one-click guide: it is the clue the
            # operator needs to adjust the click that was refused.
            self._set_camera_ray_guide_visible(True)
            self._clear_stream_rays()
            self._clear_stream_predicted()
            self._clear_external_targets()
            self._clear_stream_tip()

    @staticmethod
    def _result_key(result):
        """Geometry that decides whether a result needs drawing again."""
        def numbers(values):
            try:
                return tuple(round(float(value), 6) for value in (values or []))
            except (TypeError, ValueError):
                return ()

        pixels = result.get("pixels") or []
        return (
            result.get("status"),
            bool(result.get("demo")),
            tuple(numbers(pixel) if pixel else None for pixel in pixels),
            numbers(result.get("ras")),
            numbers(result.get("residual_px")),
        )

    def _stream_ray_node(self, index):
        name = STREAM_RAY_NODE_NAMES[index]
        node = slicer.mrmlScene.GetFirstNodeByName(name)
        if node is not None and not node.IsA("vtkMRMLMarkupsLineNode"):
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsLineNode", name)
        return node

    # -- one-click ray guide --------------------------------------------

    def _guide_rig_document(self):
        """The projections the guide needs: stream first, PNG folder second."""
        if self._stream_rig:
            return self._stream_rig
        if self.frames_folder is None:
            return None
        try:
            return json.loads(
                (pathlib.Path(self.frames_folder) / RIG_FILE).read_text(
                    encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _camera_guide_node(self, name, class_name):
        node = slicer.mrmlScene.GetFirstNodeByName(name)
        if node is not None and not node.IsA(class_name):
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass(class_name, name)
        return node

    def _clear_camera_ray_guide(self):
        """Remove every current-click ray and epipolar line."""
        for name in (*CAMERA_GUIDE_RAY_NODE_NAMES,
                     *CAMERA_GUIDE_EPIPOLAR_NODE_NAMES,
                     *CAMERA_GUIDE_LEGACY_NODE_NAMES):
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)
        self._ray_guide_message = "ray guide: click a camera view"

    def clear_camera_ray_guide(self):
        """Public clear for the panel when camera mode is turned off."""
        self._clear_camera_ray_guide()

    def ray_guide_status(self):
        """One truthful line for the panel: what the guide is showing."""
        return self._ray_guide_message

    def _set_camera_ray_guide_visible(self, visible):
        """Show or hide the one-click guide without rebuilding its nodes."""
        for name in (*CAMERA_GUIDE_RAY_NODE_NAMES,
                     *CAMERA_GUIDE_EPIPOLAR_NODE_NAMES):
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            if node is None:
                continue
            display = node.GetDisplayNode()
            if display is not None and bool(display.GetVisibility()) != bool(visible):
                display.SetVisibility(bool(visible))

    def _hide_node(self, name):
        """Hide a node; keep it so the next result can update it in place."""
        node = slicer.mrmlScene.GetFirstNodeByName(name)
        if node is not None:
            display = node.GetDisplayNode()
            if display is not None and display.GetVisibility():
                display.SetVisibility(False)

    def _clear_node(self, name, remove=False):
        """Hide a result node for a refusal, or remove it on an explicit clear."""
        node = slicer.mrmlScene.GetFirstNodeByName(name)
        if node is None:
            return
        if remove:
            slicer.mrmlScene.RemoveNode(node)
        else:
            display = node.GetDisplayNode()
            if display is not None and display.GetVisibility():
                display.SetVisibility(False)

    def _draw_camera_ray_guides(self):
        """Draw every current click's 3D ray and its epipolar lines.

        Each camera has at most one current click. A new click updates only
        that camera's ray and the lines it casts into the other pictures; the
        other cameras' guides stay. The guide is visualization, never a
        measurement. A missing calibration means no guide, not a guessed one.
        """
        document = self._guide_rig_document() or {}
        cameras = document.get("cameras") or []
        if len(cameras) < len(CAMERA_VIEWS):
            self._ray_guide_message = (
                "ray guide: unavailable — no calibration")
            return

        clicked = [(index, self.clicks.get(view))
                   for index, view in enumerate(CAMERA_VIEWS)
                   if self.clicks.get(view) is not None]
        # A cleared camera keeps no guide. Only its own nodes are touched, so
        # the normal path (three live clicks) does no removals at all.
        clicked_indices = {index for index, _pixel in clicked}
        for index in range(len(CAMERA_VIEWS)):
            if index in clicked_indices:
                continue
            stale = [CAMERA_GUIDE_RAY_NODE_NAMES[index]]
            stale.extend(
                _camera_guide_line_name(index, target)
                for target in range(len(CAMERA_VIEWS)) if target != index)
            for name in stale:
                node = slicer.mrmlScene.GetFirstNodeByName(name)
                if node is not None:
                    slicer.mrmlScene.RemoveNode(node)
        if not clicked:
            return
        try:
            rig = load_rig(PROJECT_ROOT / "rig.json")
            view_ids = self._camera_view_ids()
        except Exception as exc:  # noqa: BLE001 - a guide must not break a click
            traceback.print_exc()
            self._ray_guide_message = f"ray guide: unavailable — {exc}"
            return

        import vtk  # noqa: PLC0415

        three_d_view = self._three_d_view_id()
        drawn_rays = []
        drawn_lines = 0
        for source_index, pixel in clicked:
            source = cameras[source_index]
            projection = source.get("projection")
            if not projection:
                continue
            try:
                origin, endpoint = ray_segment(
                    projection, pixel, CAMERA_GUIDE_LENGTH_MM)
                origin_ras = rig.to_ras(origin)
                endpoint_ras = rig.to_ras(endpoint)
            except Exception:  # noqa: BLE001 - one bad click cannot hide others
                traceback.print_exc()
                continue

            points = vtk.vtkPoints()
            points.InsertNextPoint(*origin_ras)
            points.InsertNextPoint(*endpoint_ras)
            line = vtk.vtkLine()
            line.GetPointIds().SetId(0, 0)
            line.GetPointIds().SetId(1, 1)
            lines = vtk.vtkCellArray()
            lines.InsertNextCell(line)
            polydata = vtk.vtkPolyData()
            polydata.SetPoints(points)
            polydata.SetLines(lines)
            node = self._camera_guide_node(
                CAMERA_GUIDE_RAY_NODE_NAMES[source_index],
                "vtkMRMLModelNode")
            node.SetAndObservePolyData(polydata)
            display = node.GetDisplayNode()
            if display is None:
                node.CreateDefaultDisplayNodes()
                display = node.GetDisplayNode()
            self._colour_display(display, CAMERA_CLICK_COLOURS[source_index])
            display.SetVisibility(True)
            display.SetVisibility2D(False)
            display.SetVisibility3D(True)
            display.SetLineWidth(2.0)
            try:
                display.SetPointLabelsVisibility(False)
                display.SetPropertiesLabelVisibility(False)
            except Exception:  # noqa: BLE001 - display only
                pass
            if three_d_view:
                self._restrict_display_to_view(display, three_d_view)
            drawn_rays.append(CAMERA_CLICK_LABELS[source_index])

            for target_index, target in enumerate(cameras[:len(CAMERA_VIEWS)]):
                if target_index == source_index:
                    continue
                target_projection = target.get("projection")
                width = int(target.get("width") or 0)
                height = int(target.get("height") or 0)
                if not target_projection or width <= 0 or height <= 0:
                    continue
                segment = epipolar_segment(
                    projection, pixel, target_projection, width, height)
                if segment is None:
                    continue
                try:
                    first = self.pixel_to_camera_ras(
                        CAMERA_VIEWS[target_index], *segment[0])
                    second = self.pixel_to_camera_ras(
                        CAMERA_VIEWS[target_index], *segment[1])
                except ValueError:
                    continue
                guide = self._camera_guide_node(
                    _camera_guide_line_name(source_index, target_index),
                    "vtkMRMLMarkupsLineNode")
                # Updating the two points in place avoids a node rebuild on
                # every click. Removing all points and adding them back fired
                # a burst of MRML events and made each press take hundreds of
                # milliseconds, which froze the live stream.
                if guide.GetNumberOfControlPoints() == 2:
                    guide.SetNthControlPointPosition(0, *first)
                    guide.SetNthControlPointPosition(1, *second)
                else:
                    guide.RemoveAllControlPoints()
                    guide.AddControlPoint(first[0], first[1], first[2])
                    guide.AddControlPoint(second[0], second[1], second[2])
                guide_display = guide.GetDisplayNode()
                if guide_display is None:
                    guide.CreateDefaultDisplayNodes()
                    guide_display = guide.GetDisplayNode()
                self._colour_display(guide_display,
                                     CAMERA_CLICK_COLOURS[source_index])
                guide_display.SetVisibility(True)
                guide_display.SetVisibility2D(True)
                try:
                    guide_display.SetVisibility3D(False)
                except Exception:  # noqa: BLE001 - display only
                    pass
                guide_display.SetLineWidth(2.0)
                try:
                    guide_display.SetPointLabelsVisibility(False)
                    guide_display.SetPropertiesLabelVisibility(False)
                except Exception:  # noqa: BLE001 - display only
                    pass
                self._restrict_display_to_view(
                    guide_display, view_ids[target_index])
                drawn_lines += 1

        if not drawn_rays:
            self._ray_guide_message = (
                "ray guide: unavailable — no drawable ray")
            return
        ray_word = "ray" if len(drawn_rays) == 1 else "rays"
        line_word = "line" if drawn_lines == 1 else "lines"
        self._ray_guide_message = (
            f"ray guide: {', '.join(drawn_rays)} "
            f"({len(drawn_rays)} {ray_word}, "
            f"{drawn_lines} epipolar {line_word})")

    def _draw_stream_rays(self, result):
        """Draw one line of sight per camera, from its centre to the point.

        The endpoint is the point in the camera log; the origin comes from the
        camera's own projection matrix, the same ``ray_for`` the solver uses.
        Markups line nodes are visible in the 3D view and hidden in the three
        camera panes, so the measured point is not covered by a line.
        """
        rig_doc = self._stream_rig or {}
        cameras = rig_doc.get("cameras") or []
        pixels = result.get("pixels") or []
        endpoint = result.get("ras")
        if len(cameras) < len(STREAM_RAY_NODE_NAMES) or len(pixels) < 3:
            return
        if endpoint is None:
            return
        try:
            rig = load_rig(PROJECT_ROOT / "rig.json")
        except RuntimeError:
            return
        endpoint = tuple(float(value) for value in endpoint)
        for index, (camera, pixel) in enumerate(zip(cameras, pixels)):
            if pixel is None or not camera.get("projection"):
                continue
            origin, _direction = ray_for(
                camera["projection"], (float(pixel[0]), float(pixel[1])))
            origin = rig.to_ras(tuple(float(value) for value in origin))
            node = self._stream_ray_node(index)
            if node.GetNumberOfControlPoints() == 2:
                node.SetNthControlPointPosition(0, origin[0], origin[1], origin[2])
                node.SetNthControlPointPosition(
                    1, endpoint[0], endpoint[1], endpoint[2])
            else:
                node.RemoveAllControlPoints()
                node.AddControlPoint(origin[0], origin[1], origin[2])
                node.AddControlPoint(endpoint[0], endpoint[1], endpoint[2])
            display = node.GetDisplayNode()
            if display is None:
                node.CreateDefaultDisplayNodes()
                display = node.GetDisplayNode()
            self._colour_display(display, CAMERA_CLICK_COLOURS[index])
            self._unpickable(display)
            if not display.GetVisibility():
                display.SetVisibility(True)
            display.SetVisibility2D(False)
            try:
                display.SetPointLabelsVisibility(False)
                display.SetPropertiesLabelVisibility(False)
            except Exception:
                pass

    def _clear_stream_rays(self, remove=False):
        for name in STREAM_RAY_NODE_NAMES:
            self._clear_node(name, remove=remove)

    def _clear_stream_predicted(self, remove=False):
        for name in STREAM_PREDICTED_NODE_NAMES:
            self._clear_node(name, remove=remove)

    def _clear_stream_frusta(self):
        for name in (*STREAM_FRUSTUM_NODE_NAMES, *STREAM_ORIGIN_NODE_NAMES):
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)
        self._stream_cameras_drawn = False

    def _clear_external_targets(self, remove=False):
        self._clear_node(EXTERNAL_NODE_NAME, remove=remove)

    def _clear_stream_tip(self, remove=False):
        """The camera result moves VirtualTip; hide or remove it with the result."""
        self._clear_node(TIP_NODE_NAME, remove=remove)

    def _clear_stream_geometry(self):
        """Remove overlays from a previous stream before starting a new one."""
        self._clear_camera_ray_guide()
        self._clear_stream_rays(remove=True)
        self._clear_stream_predicted(remove=True)
        self._clear_stream_frusta()

    def _invalidate_stream_result(self):
        """Drop the previous solve when a camera click changes.

        The click marks move immediately; the result point, rays and predicted
        pixels belong to the previous click set. Leaving them visible reads as
        a display mismatch until the sender returns the next result.
        """
        self._stream_result = None
        try:
            self._stream_result_mtime = STREAM_RESULT_FILE.stat().st_mtime_ns
        except OSError:
            self._stream_result_mtime = None
        self._clear_stream_predicted(remove=True)
        self._clear_stream_rays(remove=True)
        self._clear_external_targets(remove=True)
        self._clear_stream_tip(remove=True)

    def _forget_stream_result(self):
        """Forget the solved result and remove every trace of it.

        The sender owns result.json; remember its current mtime so the next
        timer tick does not reload the result we just cleared. A genuinely
        new result gets a new mtime and is still picked up.
        """
        self._stream_result = None
        try:
            self._stream_result_mtime = STREAM_RESULT_FILE.stat().st_mtime_ns
        except OSError:
            self._stream_result_mtime = None
        self._clear_camera_ray_guide()
        self._clear_stream_rays(remove=True)
        self._clear_stream_predicted(remove=True)
        self._clear_external_targets(remove=True)
        self._clear_stream_tip(remove=True)

    def _stream_frustum_node(self, index):
        name = STREAM_FRUSTUM_NODE_NAMES[index]
        node = slicer.mrmlScene.GetFirstNodeByName(name)
        if node is not None and not node.IsA("vtkMRMLModelNode"):
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", name)
        return node

    def _stream_origin_node(self, index):
        name = STREAM_ORIGIN_NODE_NAMES[index]
        node = slicer.mrmlScene.GetFirstNodeByName(name)
        if node is not None and not node.IsA("vtkMRMLMarkupsFiducialNode"):
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsFiducialNode", name)
        return node

    def _draw_stream_frusta(self, rig_doc=None):
        """Show all three camera centres and their view directions.

        The origin markers answer "where is the camera"; the short frusta
        answer "where is it looking". They are rig furniture, not result
        geometry, so a refused click set or a cleared result never removes
        them. Once first drawn, they stay until the stream is disconnected.
        """
        if self._stream_cameras_drawn:
            return
        rig_doc = rig_doc if rig_doc is not None else (self._stream_rig or {})
        cameras = rig_doc.get("cameras") or []
        if len(cameras) < len(STREAM_FRUSTUM_NODE_NAMES):
            return
        try:
            rig = load_rig(PROJECT_ROOT / "rig.json")
        except RuntimeError:
            return
        import vtk  # noqa: PLC0415

        field = slicer.mrmlScene.GetFirstNodeByName("FieldCube")
        if field is not None and field.GetDisplayNode() is not None:
            self._unpickable(field.GetDisplayNode())

        drawn = 0
        for index, camera in enumerate(cameras):
            projection = camera.get("projection")
            width = int(camera.get("width") or 0)
            height = int(camera.get("height") or 0)
            if not projection or width <= 0 or height <= 0:
                continue
            corners = ((0, 0), (width - 1, 0),
                       (width - 1, height - 1), (0, height - 1))
            origin, _direction = ray_for(projection, corners[0])
            origin = tuple(float(value) for value in origin)
            origin_ras = rig.to_ras(origin)
            points = vtk.vtkPoints()
            points.InsertNextPoint(*origin_ras)
            for corner in corners:
                _same_origin, direction = ray_for(projection, corner)
                tip = tuple(origin[axis] + float(direction[axis]) * 100.0
                            for axis in range(3))
                points.InsertNextPoint(*rig.to_ras(tip))
            lines = vtk.vtkCellArray()
            for first, second in ((0, 1), (0, 2), (0, 3), (0, 4),
                                  (1, 2), (2, 3), (3, 4), (4, 1)):
                line = vtk.vtkLine()
                line.GetPointIds().SetId(0, first)
                line.GetPointIds().SetId(1, second)
                lines.InsertNextCell(line)
            polydata = vtk.vtkPolyData()
            polydata.SetPoints(points)
            polydata.SetLines(lines)
            node = self._stream_frustum_node(index)
            node.SetAndObservePolyData(polydata)
            display = node.GetDisplayNode()
            if display is None:
                node.CreateDefaultDisplayNodes()
                display = node.GetDisplayNode()
            self._colour_display(display, CAMERA_CLICK_COLOURS[index])
            self._unpickable(display)
            display.SetLineWidth(1.0)
            display.SetVisibility(True)
            try:
                display.SetVisibility2D(False)
            except Exception:
                pass

            marker = self._stream_origin_node(index)
            try:
                marker.SetLocked(False)
            except AttributeError:  # older Slicer without node-level lock
                pass
            marker.RemoveAllControlPoints()
            marker.AddControlPoint(origin_ras[0], origin_ras[1], origin_ras[2])
            marker.SetNthControlPointLabel(0, f"C{index + 1}")
            try:
                marker.SetLocked(True)
            except AttributeError:  # older Slicer without node-level lock
                pass
            marker_display = marker.GetDisplayNode()
            if marker_display is None:
                marker.CreateDefaultDisplayNodes()
                marker_display = marker.GetDisplayNode()
            self._colour_display(marker_display,
                                 CAMERA_CLICK_COLOURS[index])
            self._unpickable(marker_display)
            marker_display.SetGlyphScale(4.0)
            marker_display.SetPointLabelsVisibility(True)
            marker_display.SetPropertiesLabelVisibility(False)
            marker_display.SetVisibility(True)
            try:
                marker_display.SetVisibility2D(False)
            except Exception:
                pass
            try:
                marker_display.SetVisibility3D(True)
            except Exception:
                pass
            drawn += 1

        if drawn == len(STREAM_FRUSTUM_NODE_NAMES):
            self._stream_cameras_drawn = True
            self._fit_stream_cameras()

    def _fit_stream_cameras(self):
        """Fit the 3D view once per stream so the camera origins are visible."""
        if self._stream_cameras_fitted:
            return
        if self._three_d_view_id() is None:
            return
        try:
            slicer.util.resetThreeDViews()
        except Exception:  # noqa: BLE001 - a layout must not break the stream
            traceback.print_exc()
            return
        self._stream_cameras_fitted = True

    def _stream_predicted_node(self, index):
        name = STREAM_PREDICTED_NODE_NAMES[index]
        node = slicer.mrmlScene.GetFirstNodeByName(name)
        if node is not None and not node.IsA("vtkMRMLMarkupsFiducialNode"):
            slicer.mrmlScene.RemoveNode(node)
            node = None
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsFiducialNode", name)
        return node

    def _draw_stream_predicted(self, result):
        """Mark where the measured point reprojects in each camera picture.

        The click mark is the measurement's input; the predicted mark is what
        the solved point says the pixel should have been. The gap is the
        residual, visible without opening the 3D view.
        """
        rig_doc = self._stream_rig or {}
        cameras = rig_doc.get("cameras") or []
        pixels = result.get("pixels") or []
        rig_point = result.get("rig_point")
        if len(cameras) < 3 or len(pixels) < 3 or rig_point is None:
            return
        try:
            view_ids = self._camera_view_ids()
        except ValueError:
            return
        residuals = result.get("residual_px") or [None] * 3
        for index, (camera, _pixel) in enumerate(zip(cameras, pixels)):
            projection = camera.get("projection")
            if not projection:
                continue
            u_px, v_px = project(projection, rig_point)
            try:
                ras = self.pixel_to_camera_ras(CAMERA_VIEWS[index],
                                               float(u_px), float(v_px))
            except ValueError:
                continue
            node = self._stream_predicted_node(index)
            if node.GetNumberOfControlPoints() == 1:
                node.SetNthControlPointPosition(0, ras[0], ras[1], ras[2])
            else:
                node.RemoveAllControlPoints()
                node.AddControlPoint(ras[0], ras[1], ras[2])
            residual = residuals[index]
            label = f"pred {CAMERA_CLICK_LABELS[index]} ({u_px:.0f}, {v_px:.0f})"
            if residual is not None:
                label += f" res {float(residual):.2f} px"
            node.SetNthControlPointLabel(0, label)
            display = node.GetDisplayNode()
            if display is None:
                node.CreateDefaultDisplayNodes()
                display = node.GetDisplayNode()
            self._colour_display(display, (0.0, 0.8, 0.8))
            display.SetGlyphScale(3.0)
            display.SetPointLabelsVisibility(True)
            try:
                display.SetPropertiesLabelVisibility(False)
            except Exception:  # noqa: BLE001 - display only
                pass
            if not display.GetVisibility():
                display.SetVisibility(True)
            try:
                display.SetVisibility3D(False)
            except Exception:
                pass
            self._restrict_display_to_view(display, view_ids[index])

    def stream_result_status(self):
        """One line for the panel: truth error and per-camera residual."""
        result = self._stream_result
        if result is None:
            return {"status": "none",
                    "message": "camera result: no measurement yet",
                    "error_mm": None, "residual_px": None, "result": None}
        status = result.get("status", "unknown")
        error = result.get("error_mm")
        residuals = result.get("residual_px") or []
        if status == "found":
            if error is None:
                message = "measurement found (no truth)"
            else:
                message = f"synthetic truth: error {float(error):.2f} mm"
            if residuals:
                message += " · residual " + ", ".join(
                    f"{float(value):.2f}" for value in residuals) + " px"
        elif status == "inconsistent":
            message = result.get("message") or "click set inconsistent"
            if error is not None:
                message += (f" · truth error {float(error):.2f} mm "
                            "(diagnostic only)")
        else:
            message = (f"measurement {status}: "
                       f"{result.get('message', 'no point')}")
        if result.get("demo"):
            message = "DEMO (estimated scale) · " + message
        return {"status": status, "message": message, "error_mm": error,
                "residual_px": residuals, "result": result}

    def _write_stream_command(self, command):
        STREAM_FOLDER.mkdir(parents=True, exist_ok=True)
        temporary = STREAM_COMMAND_FILE.with_name(
            STREAM_COMMAND_FILE.name + ".tmp")
        temporary.write_text(json.dumps({
            "command": command,
            "when": datetime.now().isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
        temporary.replace(STREAM_COMMAND_FILE)
        return STREAM_COMMAND_FILE

    def pause_camera_stream(self):
        """Ask the sender to freeze after its current complete frame set."""
        if self._stream_status not in ("waiting", "connected", "disconnected"):
            return {"status": "off",
                    "message": "camera stream: not running; connect it first"}
        self._write_stream_command("pause")
        return {"status": "pause-requested",
                "message": "camera stream: pause requested; waiting for the sender"}

    def resume_camera_stream(self):
        """Ask the sender to continue from the frozen frame."""
        if self._stream_status not in ("waiting", "connected", "disconnected"):
            return {"status": "off",
                    "message": "camera stream: not running; connect it first"}
        self._write_stream_command("resume")
        return {"status": "resume-requested",
                "message": "camera stream: resume requested; waiting for the sender"}

    def step_camera_stream(self):
        """Advance one frame while paused, leaving the stream paused."""
        if self._stream_status not in ("waiting", "connected", "disconnected"):
            return {"status": "off",
                    "message": "camera stream: not running; connect it first"}
        if not (self._stream_state or {}).get("paused"):
            return {"status": "not-paused",
                    "message": "camera stream: pause first, then step"}
        self._write_stream_command("step")
        return {"status": "step-requested",
                "message": "camera stream: step requested; waiting for the sender"}

    def disconnect_camera_stream(self):
        """Stop listening and forget which views the stream owned."""
        try:
            self.stop_video()
        except Exception:  # noqa: BLE001 - a stop must not hide the disconnect
            traceback.print_exc()
        if self._stream_timer is not None:
            self._stream_timer.stop()
        connector = slicer.mrmlScene.GetFirstNodeByName(STREAM_CONNECTOR_NAME)
        if connector is not None:
            try:
                connector.Stop()
            except Exception:  # noqa: BLE001 - stop must not hide a scene error
                pass
        self._stream_bound = False
        self._stream_node_ids = ()
        self._stream_views = {}
        self._stream_status = "off"
        self._stream_message = "camera stream: off"
        self._stream_cameras_drawn = False
        self._stream_cameras_fitted = False
        self._clear_stream_geometry()
        return self.camera_stream_status()

    def _show_camera_views(self, nodes):
        """Put three scalar volumes into the three camera slice views."""
        views = {}
        for widget_name, node in zip(CAMERA_VIEWS, nodes):
            widget = slicer.app.layoutManager().sliceWidget(widget_name)
            widget.mrmlSliceNode().SetOrientationToAxial()
            composite = widget.sliceLogic().GetSliceCompositeNode()
            composite.SetBackgroundVolumeID(node.GetID())
            widget.sliceLogic().FitSliceToAll()
            try:
                # The camera pictures are 2D evidence. Showing their slice
                # planes in the 3D view adds large textured surfaces that are
                # irrelevant there (and cost render time on every drag).
                widget.mrmlSliceNode().SetSliceVisible(False)
            except Exception:  # noqa: BLE001 - display only
                pass
            views[widget_name] = node.GetName()
        return views

    def _pause_camera_stream_for_frames(self):
        """A loaded PNG folder wins over a stream the operator stopped watching.

        The stream is left connected but no longer bound to the views. A PNG
        click has no frame number, and the video sender ignores those, so a
        snapshot click can never be solved against the moving video.
        """
        if self._stream_timer is not None:
            self._stream_timer.stop()
        if self._stream_status in ("waiting", "connected", "disconnected"):
            self._stream_status = "paused"
            self._stream_message = (
                "camera stream: paused while a frame folder is shown; "
                "press Connect camera stream to return to it")

    def load_camera_views(self, folder=None):
        """Show one camera picture in each of the three slice views.

        The pictures are ordinary PNGs written by `tools/`; loading them as
        single-slice volumes is what lets Slicer's own view report a click in
        image coordinates. Nothing here knows whether the pictures came from a
        renderer or a camera.
        """
        folder = FRAMES_FOLDER if folder is None else pathlib.Path(folder)
        manifest_path = folder / "camera-frames.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except OSError as exc:
            return {"status": "nothing", "views": {}, "problems": (f"{manifest_path.name}: {exc}",)}
        except ValueError as exc:
            return {"status": "nothing", "views": {}, "problems": (f"{manifest_path.name}: {exc}",)}

        cameras = manifest.get("cameras") or []
        if len(cameras) != len(CAMERA_VIEWS):
            return {"status": "nothing", "views": {},
                    "problems": (f"{manifest_path.name}: expected {len(CAMERA_VIEWS)} cameras, "
                                 f"found {len(cameras)}",)}
        self._pause_camera_stream_for_frames()
        # Load first, assign second: each load auto-shows its volume in every
        # slice view, so assigning inside the load loop would be clobbered by
        # the next load and all three views would end on the last camera.
        nodes = []
        for camera in cameras:
            path = folder / camera["file"]
            nodes.append(slicer.util.loadVolume(
                str(path), {"name": f"Camera{camera['name'][1:]}",
                            "singleFile": True}))
        views = self._show_camera_views(nodes)
        # Remember where the pictures came from: Spawn reads camera-rig.json
        # from this same folder. Only a successful load counts -- a failed
        # probe must not clobber the folder a good load established.
        self.frames_folder = pathlib.Path(folder)
        self._clear_camera_ray_guide()
        try:
            rig_doc = self._guide_rig_document()
            if rig_doc:
                self._draw_stream_frusta(rig_doc)
            else:
                self._clear_stream_frusta()
        except Exception:  # noqa: BLE001 - pictures must still load
            traceback.print_exc()
            self._clear_stream_frusta()
        return {"status": "loaded", "views": views, "problems": ()}

    def _camera_background_node(self, widget_name):
        """The camera picture volume shown in one view, or a named refusal."""
        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        if widget is None:
            raise ValueError(f"{widget_name}: camera view is missing")
        volume_id = (widget.sliceLogic().GetSliceCompositeNode()
                     .GetBackgroundVolumeID())
        if not volume_id:
            raise ValueError(f"{widget_name}: no camera picture loaded")
        node = slicer.mrmlScene.GetNodeByID(volume_id)
        if node is None or node.GetImageData() is None:
            raise ValueError(f"{widget_name}: no camera picture loaded")
        return node

    def _click_ras(self, widget_name, x, y):
        """The RAS point under a widget click, through the view's own mapping.

        The single path camera_pixel and the camera-click marks share: the Qt
        widget pixel (origin top-left, y down) is flipped into VTK display
        coordinates (origin bottom-left, y up), then the slice node's XYToRAS
        takes it into RAS. A view with no mapping, or with no camera picture
        loaded, is refused with a ValueError naming the view.
        """
        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        node = self._camera_background_node(widget_name)
        view_height = float(widget.sliceView().size.height())
        slice_to_ras = _matrix4(widget.mrmlSliceNode().GetXYToRAS())
        if abs(_det4(slice_to_ras)) < 1e-12:
            # Same refusal targeting.clicks gives a singular view: it
            # collapses positions that must stay distinct, so there is no
            # mapping to apply.
            raise ValueError("the view has no mapping")
        ras = _apply4(slice_to_ras, (float(x), (view_height - 1.0) - float(y),
                                     0.0, 1.0))
        return float(ras[0]), float(ras[1]), float(ras[2])

    def _restrict_display_to_view(self, display, view_id):
        """Show one display node in one slice view only (never the 3D view).

        The TypeError vtkStringArray fallback is the Slicer-API spelling
        difference across builds; one helper so the click
        marks share it instead of copying it.
        """
        try:
            display.SetViewNodeIDs([view_id])
        except TypeError:
            import vtk  # noqa: PLC0415

            ids = vtk.vtkStringArray()
            ids.InsertNextValue(view_id)
            display.SetViewNodeIDs(ids)

    def _camera_view_ids(self):
        """The live slice-node id of each camera view, in CAMERA_VIEWS order.

        Resolved before any scene mutation: a missing view refuses the whole
        call with a ValueError naming the view, so the scene is never left
        half-marked. (Slicer keeps slice widgets alive across layout changes,
        so this is an API contract rather than a reachable layout.)
        """
        view_ids = []
        for widget_name in CAMERA_VIEWS:
            widget = slicer.app.layoutManager().sliceWidget(widget_name)
            if widget is None or widget.mrmlSliceNode() is None:
                raise ValueError(
                    f"{widget_name}: the {widget_name} slice view is not in "
                    "this layout — restore the standard layout")
            view_ids.append(widget.mrmlSliceNode().GetID())
        return tuple(view_ids)

    def _three_d_view_id(self):
        """The live 3D-view node id, or None when the layout has no 3D pane."""
        layout = slicer.app.layoutManager()
        if not hasattr(layout, "threeDWidget"):
            return None
        widget = layout.threeDWidget(0)
        if widget is None:
            return None
        view_node = widget.mrmlViewNode()
        return view_node.GetID() if view_node is not None else None

    def camera_clicks_nodes(self, view_ids):
        """One single-mark fiducial per camera click, each in its own view.

        Each node holds at most one control point (its camera's), coloured in
        that camera's colour, and its display node is restricted to that
        camera's slice view -- the 3D view is deliberately excluded. The
        slice-node ids are resolved by the caller (which refuses the whole
        mark if a view is missing), so this never dereferences a half-built
        layout. Guard pattern copied from targets_node(): a same-named node of
        the wrong class is removed, never shadowed. Display settings are
        applied on every call, same discipline as targets_node(), so a
        manually-retinted node returns to spec.
        """
        nodes = []
        for index, name in enumerate(CAMERA_CLICKS_NODE_NAMES):
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            if node is not None and not node.IsA("vtkMRMLMarkupsFiducialNode"):
                slicer.mrmlScene.RemoveNode(node)
                node = None
            if node is None:
                node = slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLMarkupsFiducialNode", name
                )
            display = self._display(node, CAMERA_CLICK_COLOURS[index])
            display.SetPointLabelsVisibility(True)
            display.SetGlyphScale(CAMERA_CLICK_GLYPH_SCALE)
            self._restrict_display_to_view(display, view_ids[index])
            nodes.append(node)
        legacy = slicer.mrmlScene.GetFirstNodeByName(CAMERA_CLICKS_NODE_NAME)
        if legacy is not None:
            # The pre-split single node would re-stack all three marks in
            # every pane; a mark removes it, never reuses it.
            slicer.mrmlScene.RemoveNode(legacy)
        return tuple(nodes)

    def mark_camera_click(self, widget_name, ras):
        """Drop (or move) this camera's mark at the clicked RAS.

        One single-mark node per camera view, in CAMERA_VIEWS order: the
        node's own colour is its camera's sandbox colour, and its display is
        restricted to that camera's slice view, so each pane shows exactly one
        mark. The label names the camera (Red is c1, per the camera order)
        plus the published pixel, so the mark can be matched by typing numbers
        instead of aiming the mouse. The view ids resolve before any scene
        mutation: a missing view raises ValueError naming the view and the
        scene is untouched. Self-initiated mutations run under the
        re-entrancy guard so the PointModifiedEvent observers do not
        double-publish them.
        """
        index = CAMERA_VIEWS.index(widget_name)
        nodes = self.camera_clicks_nodes(self._camera_view_ids())
        node = nodes[index]
        was = self._clicks_updating
        self._clicks_updating = True
        try:
            if node.GetNumberOfControlPoints() == 0:
                node.AddControlPoint(ras[0], ras[1], ras[2])
            else:
                node.SetNthControlPointPosition(0, ras[0], ras[1], ras[2])
            node.SetNthControlPointVisibility(0, True)
        finally:
            self._clicks_updating = was
        self.watch_camera_clicks()
        return node

    def _refresh_click_labels(self, nodes=None):
        """Rewrite every click-mark label from the live click state. Guarded.

        Each camera's node holds at most its own point at index 0. A view
        with a valid pixel reads `c1 (178, 128)`; a view whose edited
        coordinate falls outside the picture reads `c2 outside the picture`;
        anything else keeps the plain camera tag. `nodes` is the three live
        nodes in CAMERA_VIEWS order (looked up when omitted); absent or empty
        nodes have no label to write. Must run while _clicks_updating is held
        (or acquire it): label writes fire node events, and the observers
        early-return on the guard.
        """
        if nodes is None:
            nodes = [slicer.mrmlScene.GetFirstNodeByName(name)
                     for name in CAMERA_CLICKS_NODE_NAMES]
        was = self._clicks_updating
        self._clicks_updating = True
        try:
            for index, node in enumerate(nodes):
                if node is None or node.GetNumberOfControlPoints() < 1:
                    continue
                widget = CAMERA_VIEWS[index]
                tag = CAMERA_CLICK_LABELS[index]
                pixel = self.clicks.get(widget)
                if pixel is not None:
                    node.SetNthControlPointLabel(
                        0, f"{tag} ({pixel[0]:.0f}, {pixel[1]:.0f})")
                elif widget in self.click_outside:
                    node.SetNthControlPointLabel(
                        0, f"{tag} outside the picture")
                else:
                    node.SetNthControlPointLabel(0, tag)
        finally:
            self._clicks_updating = was

    def camera_status(self):
        """Truthful one-line state for the panel: `clicked n of 3` plus notes."""
        done = len(self.clicks)
        text = f"clicked {done} of 3"
        notes = [f"{CAMERA_CLICK_LABELS[CAMERA_VIEWS.index(widget)]} "
                 f"outside the picture"
                 for widget in CAMERA_VIEWS if widget in self.click_outside]
        if notes:
            text += " — " + ", ".join(notes)
        return text

    def ras_to_camera_pixel(self, widget_name, ras):
        """The picture pixel under a typed RAS point, via the volume matrix.

        A typed coordinate needs only the background volume's RASToIJK: apply
        it to (ras, 1) and read (i, j) as (u, v). The slice widget's XYToRAS
        path is for widget events, not this. Out-of-picture raises ValueError
        naming the view; the caller holds that view open as unclicked.
        """
        import vtk  # noqa: PLC0415

        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        node = self._camera_background_node(widget_name)
        ras_to_ijk = vtk.vtkMatrix4x4()
        node.GetRASToIJKMatrix(ras_to_ijk)
        index = _apply4(_matrix4(ras_to_ijk), (ras[0], ras[1], ras[2], 1.0))
        u, v = float(index[0]), float(index[1])
        width, height = node.GetImageData().GetDimensions()[0:2]
        if not (0.0 <= u < width and 0.0 <= v < height):
            raise ValueError(f"{widget_name}: the point is outside the picture "
                             f"({u:.0f}, {v:.0f} of {width}x{height})")
        return u, v

    def watch_camera_clicks(self, interval_ms=CAMERA_CLICKS_WRITE_INTERVAL_MS):
        """Republish the click set when a mark is edited anywhere.

        Every camera node carries its own PointModifiedEvent observer: a
        Data-module coordinate edit or a mouse-drag on any mark fires that
        node's event in a burst, and a single-shot timer restarted on each
        event settles the file ~150 ms after the last one (the probe-publish
        pattern). Self-initiated mutations hold _clicks_updating, on which
        the observers early-return, so label writes cannot loop.
        """
        if getattr(self, "_clicks_timer", None) is not None:
            self._observe_click_nodes()
            return self._clicks_timer
        self._clicks_timer = qt.QTimer()
        self._clicks_timer.setSingleShot(True)
        self._clicks_timer.setInterval(int(interval_ms))
        self._clicks_timer.timeout.connect(self._republish_camera_clicks_from_node)
        self._observe_click_nodes(ensure=True)
        return self._clicks_timer

    def _observe_click_nodes(self, ensure=False):
        """Attach the republish observer to each live click node, once each.

        Nodes created later (a first mark on a fresh scene) are picked up on
        the next watch call -- every mark ends with one -- by comparing node
        ids, so a replaced node is never double-observed and never missed.
        """
        observed = set(getattr(self, "_clicks_observed_ids", None) or ())
        if ensure:
            self.camera_clicks_nodes(self._camera_view_ids())
        for name in CAMERA_CLICKS_NODE_NAMES:
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            if node is None or node.GetID() in observed:
                continue
            self._observe_drag_events(
                node, self._on_camera_point_modified,
                self._republish_camera_clicks_from_node)
            observed.add(node.GetID())
        self._clicks_observed_ids = observed

    def _on_camera_point_modified(self):
        if self._clicks_updating or self._markup_drag_active:
            return
        timer = getattr(self, "_clicks_timer", None)
        if timer is not None:
            timer.start()

    def _republish_camera_clicks_from_node(self):
        """Re-derive each view's pixel from its mark and publish atomically.

        Same file shape as set_camera_click (CAMERA_VIEWS order, null for
        views without a valid click). An edited coordinate outside the
        picture contributes null and is noted; nothing is guessed. Runs in a
        Qt timer, so failures are reported to the console, never raised.
        """
        if self._clicks_updating:
            return
        was = self._clicks_updating
        try:
            self._clicks_updating = True
            try:
                for index, widget in enumerate(CAMERA_VIEWS):
                    node = slicer.mrmlScene.GetFirstNodeByName(
                        CAMERA_CLICKS_NODE_NAMES[index])
                    if node is None or node.GetNumberOfControlPoints() < 1:
                        self.clicks.pop(widget, None)
                        continue
                    position = [0.0, 0.0, 0.0]
                    node.GetNthControlPointPosition(0, position)
                    ras = (float(position[0]), float(position[1]),
                           float(position[2]))
                    try:
                        self.clicks[widget] = self.ras_to_camera_pixel(widget, ras)
                        self.click_outside.discard(widget)
                    except ValueError:
                        self.clicks.pop(widget, None)
                        self.click_outside.add(widget)
                self._refresh_click_labels()
            finally:
                self._clicks_updating = was
            self.publish_clicks()
            self._invalidate_stream_result()
            self._draw_camera_ray_guides()
        except Exception:  # noqa: BLE001 - timer slot: report, never raise
            traceback.print_exc()

    def camera_pixel(self, widget_name, x, y):
        """The picture pixel under a click in one view, or a refusal naming it.

        The RAS under the click comes from _click_ras -- the path the
        camera-click marks share -- and the background volume's RASToIJK then
        takes RAS into the array, whose first two indices are the camera's
        (u, v). The y-flip the old docstring described lives in _click_ras.
        """
        import vtk  # noqa: PLC0415

        ras = self._click_ras(widget_name, x, y)
        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        node = self._camera_background_node(widget_name)
        ras_to_ijk = vtk.vtkMatrix4x4()
        node.GetRASToIJKMatrix(ras_to_ijk)
        index = _apply4(_matrix4(ras_to_ijk), (ras[0], ras[1], ras[2], 1.0))
        u, v = float(index[0]), float(index[1])
        width, height = node.GetImageData().GetDimensions()[0:2]
        if not (0.0 <= u < width and 0.0 <= v < height):
            raise ValueError(f"{widget_name}: the click is outside the picture "
                             f"({u:.0f}, {v:.0f} of {width}x{height})")
        return u, v

    def pixel_to_camera_ras(self, widget_name, u, v):
        """The RAS point under a picture pixel, via the volume matrix.

        The typed-pixel path: the background volume's IJKToRAS applied to
        (u, v, 0, 1). Read from the volume on every call -- never a stored
        constant -- so a changed picture changes the answer. A view with no
        camera picture is refused with a ValueError naming the view.
        """
        import vtk  # noqa: PLC0415

        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        node = self._camera_background_node(widget_name)
        ijk_to_ras = vtk.vtkMatrix4x4()
        node.GetIJKToRASMatrix(ijk_to_ras)
        ras = _apply4(_matrix4(ijk_to_ras), (float(u), float(v), 0.0, 1.0))
        return float(ras[0]), float(ras[1]), float(ras[2])

    def _store_camera_click(self, widget_name, pixel, ras):
        """The single tail both click paths share: remember, mark, publish.

        set_camera_click (widget clicks) and set_camera_pixel (typed pixels)
        both resolve their (pixel, RAS) first, then funnel here -- one publish
        path, one label refresh, one re-entrancy guard (inside
        mark_camera_click / publish_clicks). Nothing here guesses. The view
        ids resolve before any state moves: a missing view raises ValueError
        naming the view and the click dict is untouched.
        """
        self._camera_view_ids()
        self.clicks[widget_name] = (float(pixel[0]), float(pixel[1]))
        self.click_outside.discard(widget_name)
        self.mark_camera_click(widget_name, ras)
        published = self.publish_clicks()
        self._invalidate_stream_result()
        try:
            self._draw_camera_ray_guides()
        except Exception:  # noqa: BLE001 - a guide must never lose the click
            traceback.print_exc()
            self._ray_guide_message = "ray guide: unavailable (draw failed)"
        return {"clicked": len(self.clicks), "published": published,
                "pixel": self.clicks[widget_name]}

    def set_camera_click(self, widget_name, x, y):
        """Remember one view's click, mark it, and publish the live set.

        Every successful click publishes: views not clicked yet go out as
        null, so the sandbox follows one camera at a time. A refused click
        raises ValueError naming the view and changes nothing -- the RAS and
        the pixel are both resolved before any state moves.
        """
        ras = self._click_ras(widget_name, x, y)
        pixel = self.camera_pixel(widget_name, x, y)
        return self._store_camera_click(widget_name, pixel, ras)

    def set_camera_pixel(self, widget_name, u, v):
        """Remember one view's typed pixel, mark it, and publish the live set.

        The panel path: (u, v) is already a picture pixel, so RAS comes from
        pixel_to_camera_ras (the volume's IJKToRAS), not from the slice
        widget's XYToRAS. Out-of-picture values raise ValueError naming the
        view and change nothing -- no clamping, no silent accept.
        """
        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        node = self._camera_background_node(widget_name)
        u, v = float(u), float(v)
        width, height = node.GetImageData().GetDimensions()[0:2]
        if not (0.0 <= u < width and 0.0 <= v < height):
            raise ValueError(f"{widget_name}: the point is outside the picture "
                             f"({u:.1f}, {v:.1f} of {width}x{height})")
        ras = self.pixel_to_camera_ras(widget_name, u, v)
        return self._store_camera_click(widget_name, (u, v), ras)

    def publish_clicks(self, logs_dir=None):
        """Write the live clicks atomically, in camera order.

        Views not clicked yet go out as null -- a partial set is the normal
        state after any click but the third -- and the all-null write is the
        explicit clear signal. Atomic because the sandbox polls this file.
        """
        pixels = []
        for widget in CAMERA_VIEWS:
            pixel = self.clicks.get(widget)
            pixels.append([float(pixel[0]), float(pixel[1])]
                          if pixel is not None else None)
        folder = LOGS_DIR if logs_dir is None else pathlib.Path(logs_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / CLICKS_FILE
        temporary = path.with_name(path.name + ".tmp")
        frame_id = None
        if (self._stream_status in ("connected", "disconnected")
                and self._stream_state):
            frame_id = self._stream_state.get("frame_id")
        payload = {"pixels": pixels,
                   "frame_id": frame_id,
                   "when": datetime.now().isoformat(timespec="seconds")}
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
        self.last_clicks_path = path
        self._refresh_click_labels()
        return path

    def clear_camera_clicks(self, logs_dir=None):
        """Forget the clicks and the solved result, and publish the empty set.

        The file carries the clear -- all null -- so the sandbox follows; the
        marks, solved point, rays, frusta and predicted pixels go so the scene
        reads clean. Clearing touches control points only, so it needs no views
        and works with no widgets open; a leftover pre-split node is removed
        here too, never reused.
        """
        was = self._clicks_updating
        self._clicks_updating = True
        try:
            self.clicks = {}
            self.click_outside = set()
            for name in CAMERA_CLICKS_NODE_NAMES:
                node = slicer.mrmlScene.GetFirstNodeByName(name)
                if node is not None:
                    node.RemoveAllControlPoints()
            legacy = slicer.mrmlScene.GetFirstNodeByName(CAMERA_CLICKS_NODE_NAME)
            if legacy is not None:
                slicer.mrmlScene.RemoveNode(legacy)
        finally:
            self._clicks_updating = was
        self._clear_camera_ray_guide()
        self._forget_stream_result()
        return self.publish_clicks(logs_dir=logs_dir)

    def _remove_legacy_spawn_nodes(self):
        """Remove any pre-C2 spawn marks, never reuse them.

        Spawn used to keep its own SandboxSpawnedPoint* nodes; it now seeds
        the camera clicks instead, so these nodes are only ever removed.
        """
        for name in _LEGACY_SPAWN_NODE_NAMES:
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)

    def spawn_point(self, x, y, z, folder=None):
        """Seed the three camera clicks from a typed rig-mm 3D point.

        The projection comes from the `camera-rig.json` file next to the frame
        PNGs -- the same frames folder `load_camera_views` used -- never from
        re-derived rig/optics. Each in-bounds camera's click slot takes the
        projected pixel (its mark moves to the RAS under that pixel, via
        `pixel_to_camera_ras`); an out-of-bounds camera's slot becomes None
        (its mark is removed, its pixel row is cleared). This REPLACES the
        whole click set in one store+publish pass, so afterwards the marks
        are ordinary editable points: drag, type a row, or edit the Data
        table and the sandbox rays/point follow.

        Returns `{"status": "spawned", ...}` or `{"status": "refused",
        "message": ...}`; a refusal changes nothing in the scene.
        """
        import numpy  # noqa: PLC0415 - Slicer bundles numpy; the file needs it

        point = (float(x), float(y), float(z))
        if folder is None:
            folder = getattr(self, "frames_folder", None)
        if folder is None:
            return {"status": "refused",
                    "message": ("spawn: no frames folder known — load the camera views "
                                "first (their folder also holds camera-rig.json)")}
        rig_path = pathlib.Path(folder) / RIG_FILE
        try:
            payload = json.loads(rig_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "refused",
                    "message": ("spawn: no camera-rig.json found next to the frames — "
                                "regenerate with tools/camera_sandbox.py --write-frames "
                                "on that folder")}
        cameras = (payload.get("cameras") if isinstance(payload, dict) else None) or []
        if len(cameras) != len(CAMERA_VIEWS):
            return {"status": "refused",
                    "message": (f"spawn: {RIG_FILE} holds {len(cameras)} cameras, "
                                f"need {len(CAMERA_VIEWS)} -- not guessing")}
        pixels = []
        for entry in cameras:
            try:
                matrix = numpy.asarray(entry["projection"], float)
                width, height = int(entry["width"]), int(entry["height"])
            except (KeyError, TypeError, ValueError):
                return {"status": "refused",
                        "message": (f"spawn: {RIG_FILE} entry "
                                    f"{entry.get('name', '?')!r} is unreadable "
                                    "-- not guessing")}
            if matrix.shape != (3, 4) or not bool(numpy.all(numpy.isfinite(matrix))):
                return {"status": "refused",
                        "message": (f"spawn: {RIG_FILE} entry "
                                    f"{entry.get('name', '?')!r} has no usable "
                                    "projection -- not guessing")}
            weighed = matrix @ numpy.array([point[0], point[1], point[2], 1.0])
            if (not bool(numpy.all(numpy.isfinite(weighed)))
                    or float(weighed[2]) <= 0.0):
                # No valid image when the point is on or behind the camera.
                pixels.append(None)
                continue
            u, v = float(weighed[0] / weighed[2]), float(weighed[1] / weighed[2])
            if 0.0 <= u < width and 0.0 <= v < height:
                pixels.append((u, v))
            else:
                pixels.append(None)
        # Resolve every RAS before touching the scene: a view with no picture
        # refuses the whole spawn with the clicks exactly as they were.
        ras_points = []
        for widget_name, pixel in zip(CAMERA_VIEWS, pixels):
            if pixel is None:
                ras_points.append(None)
                continue
            try:
                ras_points.append(self.pixel_to_camera_ras(
                    widget_name, pixel[0], pixel[1]))
            except ValueError as exc:
                return {"status": "refused", "message": f"spawn: {exc}"}
        # The display restriction needs each view's live slice node; a
        # missing view refuses the whole spawn here, before the scene changes.
        try:
            view_ids = self._camera_view_ids()
        except ValueError as exc:
            return {"status": "refused",
                    "message": (f"spawn: {exc} — restore the standard layout")}
        nodes = self.camera_clicks_nodes(view_ids)
        self._remove_legacy_spawn_nodes()
        was = self._clicks_updating
        self._clicks_updating = True
        try:
            self.clicks = {}
            self.click_outside = set()
            for index, (widget_name, pixel, ras) in enumerate(
                    zip(CAMERA_VIEWS, pixels, ras_points)):
                node = nodes[index]
                if pixel is None or ras is None:
                    node.RemoveAllControlPoints()
                    self.click_outside.add(widget_name)
                    continue
                self.clicks[widget_name] = (float(pixel[0]), float(pixel[1]))
                if node.GetNumberOfControlPoints() == 0:
                    node.AddControlPoint(ras[0], ras[1], ras[2])
                else:
                    node.SetNthControlPointPosition(0, ras[0], ras[1], ras[2])
                node.SetNthControlPointVisibility(0, True)
            self._refresh_click_labels(nodes)
        finally:
            self._clicks_updating = was
        self.watch_camera_clicks()
        self.publish_clicks()
        self._draw_camera_ray_guides()
        return {"status": "spawned", "point": point, "pixels": tuple(pixels),
                "placed": sum(1 for pixel in pixels if pixel is not None)}

    def clear_targets(self):
        node = slicer.mrmlScene.GetFirstNodeByName(TARGETS_NODE_NAME)
        if node is not None:
            node.RemoveAllControlPoints()
        tip = slicer.mrmlScene.GetFirstNodeByName(TIP_NODE_NAME)
        if tip is not None:
            slicer.mrmlScene.RemoveNode(tip)
        self.placed = 0
        self.rejected = 0
