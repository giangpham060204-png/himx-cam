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
        for name in ("_probe_timer", "_clicks_timer"):
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
        # Remember where the pictures came from: Spawn reads camera-rig.json
        # from this same folder. Only a successful load counts -- a failed
        # probe must not clobber the folder a good load established.
        self.frames_folder = pathlib.Path(folder)
        return {"status": "loaded", "views": views, "problems": ()}

    def _click_ras(self, widget_name, x, y):
        """The RAS point under a widget click, through the view's own mapping.

        The single path camera_pixel and the camera-click marks share: the Qt
        widget pixel (origin top-left, y down) is flipped into VTK display
        coordinates (origin bottom-left, y up), then the slice node's XYToRAS
        takes it into RAS. A view with no mapping, or with no camera picture
        loaded, is refused with a ValueError naming the view.
        """
        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        node = slicer.mrmlScene.GetNodeByID(
            widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID())
        if node is None:
            raise ValueError(f"{widget_name}: no camera picture loaded")
        if node.GetImageData() is None:
            raise ValueError(f"{widget_name}: no camera picture loaded")
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
        node = slicer.mrmlScene.GetNodeByID(
            widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID())
        if node is None or node.GetImageData() is None:
            raise ValueError(f"{widget_name}: no camera picture loaded")
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
            node.AddObserver(node.PointModifiedEvent,
                             lambda *_args: self._on_camera_point_modified())
            observed.add(node.GetID())
        self._clicks_observed_ids = observed

    def _on_camera_point_modified(self):
        if self._clicks_updating:
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
        node = slicer.mrmlScene.GetNodeByID(
            widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID())
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
        node = slicer.mrmlScene.GetNodeByID(
            widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID())
        if node is None or node.GetImageData() is None:
            raise ValueError(f"{widget_name}: no camera picture loaded")
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
        node = slicer.mrmlScene.GetNodeByID(
            widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID())
        if node is None or node.GetImageData() is None:
            raise ValueError(f"{widget_name}: no camera picture loaded")
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
        payload = {"pixels": pixels,
                   "when": datetime.now().isoformat(timespec="seconds")}
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
        self.last_clicks_path = path
        self._refresh_click_labels()
        return path

    def clear_camera_clicks(self, logs_dir=None):
        """Forget the clicks, publish the empty set, and remove the marks.

        The file carries the clear -- all null -- so the sandbox follows;
        the marks go so the panel reads clean. Clearing touches control
        points only, so it needs no views and works with no widgets open; a
        leftover pre-split node is removed here too, never reused.
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
