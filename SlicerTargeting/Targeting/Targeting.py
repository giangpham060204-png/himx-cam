"""The Targeting module: click a point in a slice view, get a 3D target.

The module adds no maths. It reads Slicer's live view mapping, passes the
click through the verified converter, then shows and records the result.
"""

import pathlib
import sys

import ctk
import qt
import slicer
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleWidget,
)

_THIS_DIR = pathlib.Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

PROJECT_ROOT = _THIS_DIR.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from TargetingLib.logic import (  # noqa: E402
    LOGS_DIR,
    VIEW_WIDGETS,
    SliceClickFilter,
    TargetingLogic,
)


class Targeting(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        parent.title = "Targeting"
        parent.categories = ["HIMX"]
        parent.dependencies = []
        parent.contributors = ["himx-cam project"]
        parent.helpText = (
            "Click a point inside the operating field in the Red, Green or "
            "Yellow slice view; the click becomes a 3D target. Depth comes "
            "from the slice slider of the view you click in."
        )
        parent.acknowledgementText = ""


class TargetingWidget(ScriptedLoadableModuleWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.logic = TargetingLogic()
        self.filters = {}
        self.lastClick = None

    # -- UI ---------------------------------------------------------------

    def setup(self):
        super().setup()

        panel = ctk.ctkCollapsibleButton()
        panel.text = "Targeting"
        self.layout.addWidget(panel)
        form = qt.QFormLayout(panel)

        self.statusLabel = qt.QLabel("Open the module to read the live mapping.")
        self.statusLabel.wordWrap = True
        form.addRow(self.statusLabel)

        self.pickBox = qt.QCheckBox("Pick targets (clicks in Red/Green/Yellow)")
        form.addRow(self.pickBox)

        self.probeBox = qt.QCheckBox(
            "Probe mode (click a slice to move the camera-check point)"
        )
        form.addRow(self.probeBox)

        self.cameraBox = qt.QCheckBox("Camera view clicks: one per view")
        form.addRow(self.cameraBox)

        self.probeLabel = qt.QLabel("no probe yet")
        self.probeLabel.wordWrap = True
        form.addRow(self.probeLabel)

        self.lastLabel = qt.QLabel("no target yet")
        form.addRow(self.lastLabel)

        self.countsLabel = qt.QLabel("Placed: 0     Rejected: 0")
        form.addRow(self.countsLabel)

        buttons = qt.QHBoxLayout()
        self.clearButton = qt.QPushButton("Clear targets")
        self.openLogButton = qt.QPushButton("Open log folder")
        buttons.addWidget(self.clearButton)
        buttons.addWidget(self.openLogButton)
        form.addRow(buttons)

        external = qt.QHBoxLayout()
        self.loadExternalButton = qt.QPushButton("Load external targets")
        external.addWidget(self.loadExternalButton)
        form.addRow(external)

        self.externalLabel = qt.QLabel("no camera targets loaded")
        self.externalLabel.wordWrap = True
        form.addRow(self.externalLabel)

        camera = qt.QHBoxLayout()
        self.loadCameraViewsButton = qt.QPushButton("Load camera views")
        self.clearCameraButton = qt.QPushButton("Clear camera clicks")
        camera.addWidget(self.loadCameraViewsButton)
        camera.addWidget(self.clearCameraButton)
        form.addRow(camera)

        self.cameraLabel = qt.QLabel("no camera views loaded")
        self.cameraLabel.wordWrap = True
        form.addRow(self.cameraLabel)

        self.pickBox.toggled.connect(self.onPickToggled)
        self.probeBox.toggled.connect(self.onProbeToggled)
        self.cameraBox.toggled.connect(self.onCameraToggled)
        self.clearButton.clicked.connect(self.onClear)
        self.openLogButton.clicked.connect(self.onOpenLog)
        self.loadExternalButton.clicked.connect(self.onLoadExternal)
        self.loadCameraViewsButton.clicked.connect(self.onLoadCameraViews)
        self.clearCameraButton.clicked.connect(self.onClearCameraClicks)

        self.layout.addStretch(1)

    # -- lifecycle ----------------------------------------------------------

    def enter(self):
        self.install_filters()
        self.refresh_status()

    def exit(self):
        self.logic.stop_watch()
        self.set_pick_enabled(False)
        self.remove_filters()

    def cleanup(self):
        self.remove_filters()

    def install_filters(self):
        if self.filters:
            return
        layout = slicer.app.layoutManager()
        for view, widget_name in VIEW_WIDGETS.items():
            slice_widget = layout.sliceWidget(widget_name)
            if slice_widget is None:
                continue
            view_widget = slice_widget.sliceView()
            view_filter = SliceClickFilter(view, self.on_click)
            view_widget.installEventFilter(view_filter)
            self.filters[widget_name] = (view_widget, view_filter)

    def remove_filters(self):
        for view_widget, view_filter in self.filters.values():
            view_widget.removeEventFilter(view_filter)
        self.filters = {}
        self.pickBox.checked = False  # toggled() flips the enabled flags
        self.probeBox.checked = False
        self.cameraBox.checked = False

    def set_pick_enabled(self, enabled):
        for _widget, view_filter in self.filters.values():
            view_filter.enabled = bool(enabled)

    # -- handlers -------------------------------------------------------------

    def refresh_filters(self):
        """Clicks are wanted when any mode is on; all modes share the filters."""
        self.set_pick_enabled(self.pickBox.checked or self.probeBox.checked
                              or self.cameraBox.checked)

    def onPickToggled(self, checked):
        self.refresh_filters()

    def onCameraToggled(self, checked):
        self.refresh_filters()
        if checked:
            self.cameraLabel.text = ("camera mode: click once in Red, Green "
                                     "and Yellow, and the three pixels publish")

    def onProbeToggled(self, checked):
        self.refresh_filters()
        if checked:
            self.logic.watch_probe()
            self.probeLabel.text = ("probe mode: click a slice, or drag the "
                                    "SandboxProbe point, and the sandbox follows")
        else:
            self.logic.stop_watch()

    def on_click(self, view, u_px, v_px):
        self.lastClick = (view, u_px, v_px)
        if self.cameraBox.checked:
            self.on_camera_click(view, u_px, v_px)
            return
        if self.probeBox.checked:
            self.on_probe_click(view, u_px, v_px)
            return
        try:
            result = self.logic.place_from_click(view, u_px, v_px)
        except Exception as exc:  # noqa: BLE001 - a click must never crash Slicer
            import traceback  # noqa: PLC0415

            traceback.print_exc()
            self.statusLabel.text = f"Internal error: {exc} (details in console)"
            return

        if result["status"] == "placed":
            cube = result["cube"]
            ras = result["ras"]
            self.lastLabel.text = (
                f"Last: {view} view, depth {result['depth_mm']:.1f} mm\n"
                f"Cube ({cube[0]:.1f}, {cube[1]:.1f}, {cube[2]:.1f}) mm\n"
                f"RAS  ({ras[0]:.1f}, {ras[1]:.1f}, {ras[2]:.1f}) mm"
            )
            if result["log_error"]:
                self.statusLabel.text = (
                    f"Placed, but the log write failed: {result['log_error']}"
                )
            else:
                self.statusLabel.text = (
                    "Placed. Move the slice slider, click the next point."
                )
        else:
            self.statusLabel.text = f"Outside the field: {result['message']}"
        self.update_counts()

    def on_probe_click(self, view, u_px, v_px):
        """A click in probe mode moves the point the sandbox follows.

        No target is placed and nothing is logged: a probe is a question the
        operator is asking the cameras, and the camera log holds measurements.
        """
        try:
            result = self.logic.set_probe_from_click(view, u_px, v_px)
        except Exception as exc:        # noqa: BLE001 - a click must never crash Slicer
            import traceback  # noqa: PLC0415

            traceback.print_exc()
            self.probeLabel.text = f"probe failed: {exc}"
            return
        cube = result["cube"]
        self.probeLabel.text = (
            f"probe from {view} view, depth {result['depth_mm']:.1f} mm\n"
            f"Cube ({cube[0]:.1f}, {cube[1]:.1f}, {cube[2]:.1f}) mm"
        )

    def on_camera_click(self, view, u_px, v_px):
        """A click in camera mode becomes that camera's image pixel.

        Three clicks -- one per view -- publish atomically. A refused click
        names its view on the label instead of crashing Slicer.
        """
        widget_name = VIEW_WIDGETS[view]
        try:
            result = self.logic.set_camera_click(widget_name, u_px, v_px)
        except ValueError as exc:
            self.cameraLabel.text = str(exc)
            return
        except Exception as exc:  # noqa: BLE001 - a click must never crash Slicer
            import traceback  # noqa: PLC0415

            traceback.print_exc()
            self.cameraLabel.text = f"Internal error: {exc} (details in console)"
            return
        done = result["clicked"]
        u, v = result["pixel"]
        if result["published"] is not None:
            self.cameraLabel.text = (
                f"clicked {done} of 3 — published {result['published'].name}"
            )
        else:
            self.cameraLabel.text = (
                f"clicked {done} of 3 ({widget_name} pixel ({u:.1f}, {v:.1f}))"
            )

    def onClearCameraClicks(self, checked=False):
        self.logic.clear_camera_clicks()
        self.cameraLabel.text = "camera clicks cleared (0 of 3)"

    def update_counts(self):
        self.countsLabel.text = (
            f"Placed: {self.logic.placed}     Rejected: {self.logic.rejected}"
        )

    def onClear(self, checked=False):
        self.logic.clear_targets()
        self.lastLabel.text = "no target yet"
        self.update_counts()

    def onOpenLog(self, checked=False):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(str(LOGS_DIR)))

    def onLoadExternal(self):
        try:
            result = self.logic.load_external_targets()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.externalLabel.text = f"could not load: {exc}"
            return
        if result["status"] == "nothing":
            self.externalLabel.text = (
                f"no camera targets to show ({result['skipped']} row(s) skipped)"
            )
        else:
            self.externalLabel.text = (
                f"loaded {result['loaded']} camera target(s), "
                f"{result['skipped']} row(s) skipped"
            )
        if result["problems"]:
            self.externalLabel.text += f" — first: {result['problems'][0]}"

    def onLoadCameraViews(self):
        try:
            result = self.logic.load_camera_views()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.cameraLabel.text = f"could not load: {exc}"
            return
        if result["status"] == "nothing":
            first = result["problems"][0] if result["problems"] else "no frames found"
            self.cameraLabel.text = f"no camera views loaded — {first}"
        else:
            self.cameraLabel.text = f"loaded {', '.join(result['views'].values())}"
            if result["problems"]:
                self.cameraLabel.text += f" — first: {result['problems'][0]}"

    def refresh_status(self):
        self.statusLabel.text = "Ready - tick Pick targets and click in a view."
