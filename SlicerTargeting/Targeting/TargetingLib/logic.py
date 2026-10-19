"""Slicer-facing logic for the Targeting module.

No GUI code lives here: the widget calls these methods and displays what
they return. This module is the only new place that touches Slicer, so
everything testable stays testable.
"""

from __future__ import annotations

import json
import pathlib
import sys
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
from targeting.camera_log import FILE_PREFIX, read_camera_targets  # noqa: E402
from targeting.clicks import CLICKS_FILE, pixel_from_view  # noqa: E402
from targeting.converter import (  # noqa: E402
    OutOfFieldError,
    check_in_field_ras,
    convert,
    cube_to_ras,
    ras_to_cube,
)
from targeting.logbook import append_target  # noqa: E402
from targeting.probe import PROBE_FILE  # noqa: E402

LOGS_DIR = PROJECT_ROOT / "logs"
TIP_NODE_NAME = "VirtualTip"
TARGETS_NODE_NAME = "Targets"
EXTERNAL_NODE_NAME = "ExternalTargets"
EXTERNAL_COLOUR = (1.0, 0.55, 0.1)
VIEW_WIDGETS = {"XY": "Red", "XZ": "Green", "YZ": "Yellow"}
PROBE_NODE_NAME = "SandboxProbe"
PROBE_COLOUR = (0.85, 0.1, 0.85)
PROBE_WRITE_INTERVAL_MS = 150
CAMERA_VIEWS = ("Red", "Green", "Yellow")
FRAMES_FOLDER = LOGS_DIR / "camera-frames"


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


def _matrix4(vtk_matrix):
    """A vtkMatrix4x4 as nested tuples, the shape targeting.clicks reads."""
    return tuple(tuple(float(vtk_matrix.GetElement(row, column)) for column in range(4))
                 for row in range(4))


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
        """Publish the probe whenever it moves, coalescing a drag's burst.

        A drag fires hundreds of events. A single-shot timer restarted on each
        one writes the final position about `interval_ms` after the movement
        stops: the last position always lands, and nothing is written a hundred
        times a second.
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
        node.AddObserver(node.PointModifiedEvent,
                         lambda *_args: self._probe_timer.start())
        return self._probe_timer

    def stop_watch(self):
        timer = getattr(self, "_probe_timer", None)
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
        # Load first, assign second: each load auto-shows its volume in every
        # slice view, so assigning inside the load loop would be clobbered by
        # the next load and all three views would end on the last camera.
        nodes = []
        for camera in cameras:
            path = folder / camera["file"]
            nodes.append(slicer.util.loadVolume(
                str(path), {"name": f"Camera{camera['name'][1:]}",
                            "singleFile": True}))
        views = {}
        for widget_name, node in zip(CAMERA_VIEWS, nodes):
            widget = slicer.app.layoutManager().sliceWidget(widget_name)
            composite = widget.sliceLogic().GetSliceCompositeNode()
            composite.SetBackgroundVolumeID(node.GetID())
            widget.mrmlSliceNode().SetOrientationToAxial()
            widget.sliceLogic().FitSliceToAll()
            views[widget_name] = node.GetName()
        return {"status": "loaded", "views": views, "problems": ()}

    def camera_pixel(self, widget_name, x, y):
        """The picture pixel under a click in one view, or a refusal naming it.

        The two matrices come from Slicer: the slice node's XYToRAS (view
        coordinates into RAS) and the background volume's RASToIJK. The
        arithmetic lives in targeting.clicks, where it can be tested with
        synthetic matrices.

        XYToRAS reads VTK display coordinates (origin bottom-left, y up),
        while the click arrives as Qt widget coordinates (origin top-left, y
        down) -- the same flip the click-path smoke applies before
        SetEventPosition -- so y is flipped by the view height first.
        """
        import vtk  # noqa: PLC0415

        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        node = slicer.mrmlScene.GetNodeByID(
            widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID())
        if node is None:
            raise ValueError(f"{widget_name}: no camera picture loaded")
        if node.GetImageData() is None:
            raise ValueError(f"{widget_name}: no camera picture loaded")
        view_height = float(widget.sliceView().size.height())
        slice_to_ras = _matrix4(widget.mrmlSliceNode().GetXYToRAS())
        ras_to_ijk = vtk.vtkMatrix4x4()
        node.GetRASToIJKMatrix(ras_to_ijk)
        u, v = pixel_from_view(slice_to_ras, _matrix4(ras_to_ijk),
                               x, (view_height - 1.0) - y)
        width, height = node.GetImageData().GetDimensions()[0:2]
        if not (0.0 <= u < width and 0.0 <= v < height):
            raise ValueError(f"{widget_name}: the click is outside the picture "
                             f"({u:.0f}, {v:.0f} of {width}x{height})")
        return u, v

    def set_camera_click(self, widget_name, x, y):
        """Remember one view's click; publish once all three are in."""
        self.clicks[widget_name] = self.camera_pixel(widget_name, x, y)
        published = self.publish_clicks() if len(self.clicks) == len(CAMERA_VIEWS) else None
        return {"clicked": len(self.clicks), "published": published,
                "pixel": self.clicks[widget_name]}

    def publish_clicks(self, logs_dir=None):
        """Write the three pixels atomically, in camera order, or nothing.

        A set is all three or nothing: two views is not the measurement the
        operator thinks they made. Atomic because the sandbox polls this file.
        """
        if len(self.clicks) != len(CAMERA_VIEWS):
            return None
        pixels = [self.clicks[widget] for widget in CAMERA_VIEWS]
        folder = LOGS_DIR if logs_dir is None else pathlib.Path(logs_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / CLICKS_FILE
        temporary = path.with_name(path.name + ".tmp")
        payload = {"pixels": [[float(u), float(v)] for u, v in pixels],
                   "when": datetime.now().isoformat(timespec="seconds")}
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
        self.last_clicks_path = path
        return path

    def clear_camera_clicks(self):
        """Forget the clicks gathered so far."""
        self.clicks = {}

    def clear_targets(self):
        node = slicer.mrmlScene.GetFirstNodeByName(TARGETS_NODE_NAME)
        if node is not None:
            node.RemoveAllControlPoints()
        tip = slicer.mrmlScene.GetFirstNodeByName(TIP_NODE_NAME)
        if tip is not None:
            slicer.mrmlScene.RemoveNode(tip)
        self.placed = 0
        self.rejected = 0
