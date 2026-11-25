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
    CAMERA_CLICK_LABELS,
    CAMERA_VIEWS,
    LOGS_DIR,
    STREAM_PORT,
    VIEW_WIDGETS,
    SliceClickFilter,
    TargetingLogic,
    ThreeDViewClickFilter,
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
        self.streamTimer = None
        self.threeDFilter = None
        self._video_demo_mode = False

    # -- UI ---------------------------------------------------------------

    def setup(self):
        super().setup()

        panel = ctk.ctkCollapsibleButton()
        panel.text = "Targeting"
        self.layout.addWidget(panel)
        form = qt.QFormLayout(panel)

        self.statusLabel = qt.QLabel(
            "Connect the real cameras, then declare the marker plate below. "
            "Apply draws it in the cube and checks it against camera 1.")
        self.statusLabel.wordWrap = True
        form.addRow(self.statusLabel)

        # The camera-click (triangulation) controls moved to Advanced: the
        # marker plate is the workflow in use.

        real = qt.QHBoxLayout()
        self.realCameraButton = qt.QPushButton("Connect real cameras")
        self.realCameraButton.toolTip = (
            "Start the Pico camera sender if it is not already running, then "
            "connect c1/c2/c3. A camera that is missing shows black")
        self.realCameraStopButton = qt.QPushButton("Stop real cameras")
        self.realCameraStopButton.setEnabled(False)
        real.addWidget(self.realCameraButton)
        real.addWidget(self.realCameraStopButton)
        form.addRow(real)

        self.realCameraDeviceEdit = qt.QLineEdit("/dev/video0")
        self.realCameraDeviceEdit.toolTip = (
            "UVC device carrying the three camera tiles (480x120: c1 | c2 | c3)")
        form.addRow("camera device:", self.realCameraDeviceEdit)

        # Mock-video controls (Start video..., clip pickers, presets) are gone:
        # the rig streams from the real Pico cameras. The status label stays
        # because it also reports the camera sender.
        self.videoLabel = qt.QLabel("cameras: off")
        self.videoLabel.wordWrap = True
        form.addRow(self.videoLabel)

        self.videoSettings = ctk.ctkCollapsibleButton()
        self.videoSettings.text = "Camera settings"
        self.videoSettings.collapsed = False
        form.addRow(self.videoSettings)
        video_form = qt.QFormLayout(self.videoSettings)

        self.fpsSpin = qt.QDoubleSpinBox()
        self.fpsSpin.setRange(1.0, 60.0)
        self.fpsSpin.setDecimals(1)
        self.fpsSpin.setValue(5.0)
        self.fpsSpin.setSuffix(" fps")
        video_form.addRow("Frame rate", self.fpsSpin)

        self.portSpin = qt.QSpinBox()
        self.portSpin.setRange(1, 65535)
        self.portSpin.setValue(int(STREAM_PORT))
        video_form.addRow("Stream port", self.portSpin)

        self.videoNote = qt.QLabel("")
        self.videoNote.wordWrap = True
        video_form.addRow(self.videoNote)

        stream_control = qt.QHBoxLayout()
        self.pauseStreamButton = qt.QPushButton("Pause stream")
        self.stepStreamButton = qt.QPushButton("Step frame")
        self.resumeStreamButton = qt.QPushButton("Resume stream")
        stream_control.addWidget(self.pauseStreamButton)
        stream_control.addWidget(self.stepStreamButton)
        stream_control.addWidget(self.resumeStreamButton)
        form.addRow(stream_control)

        self.streamLabel = qt.QLabel("camera stream: off")
        self.streamLabel.wordWrap = True
        form.addRow(self.streamLabel)

        log_row = qt.QHBoxLayout()
        self.openLogButton = qt.QPushButton("Open log folder")
        log_row.addWidget(self.openLogButton)
        form.addRow(log_row)

        # -- the marker plate -------------------------------------------
        # Everything the markers need, so the JSON never has to be edited by
        # hand: size, placement, one row per marker, apply/save/load.
        self.markerPanel = ctk.ctkCollapsibleButton()
        self.markerPanel.text = "Marker plate"
        self.markerPanel.collapsed = False
        form.addRow(self.markerPanel)
        marker_form = qt.QFormLayout(self.markerPanel)

        size_row = qt.QHBoxLayout()
        self.markerWidthSpin = qt.QDoubleSpinBox()
        self.markerWidthSpin.setRange(1.0, 1000.0)
        self.markerWidthSpin.setDecimals(2)
        self.markerWidthSpin.setValue(60.0)
        self.markerWidthSpin.setSuffix(" mm")
        self.markerHeightSpin = qt.QDoubleSpinBox()
        self.markerHeightSpin.setRange(1.0, 1000.0)
        self.markerHeightSpin.setDecimals(2)
        self.markerHeightSpin.setValue(40.0)
        self.markerHeightSpin.setSuffix(" mm")
        size_row.addWidget(qt.QLabel("width"))
        size_row.addWidget(self.markerWidthSpin)
        size_row.addWidget(qt.QLabel("height"))
        size_row.addWidget(self.markerHeightSpin)
        marker_form.addRow("plate size", size_row)

        origin_row = qt.QHBoxLayout()
        self.markerOriginXSpin = qt.QDoubleSpinBox()
        self.markerOriginYSpin = qt.QDoubleSpinBox()
        self.markerOriginZSpin = qt.QDoubleSpinBox()
        for spin, value in ((self.markerOriginXSpin, 20.0),
                            (self.markerOriginYSpin, 30.0),
                            (self.markerOriginZSpin, 0.0)):
            spin.setRange(-1000.0, 1000.0)
            spin.setDecimals(2)
            spin.setValue(value)
            spin.setSuffix(" mm")
        origin_row.addWidget(qt.QLabel("x"))
        origin_row.addWidget(self.markerOriginXSpin)
        origin_row.addWidget(qt.QLabel("y"))
        origin_row.addWidget(self.markerOriginYSpin)
        origin_row.addWidget(qt.QLabel("z"))
        origin_row.addWidget(self.markerOriginZSpin)
        marker_form.addRow("cube origin", origin_row)

        self.markerTable = qt.QTableWidget(0, 4)
        self.markerTable.setHorizontalHeaderLabels(
            ["label", "x mm", "y mm", "z mm"])
        try:
            self.markerTable.horizontalHeader().setSectionResizeMode(
                0, qt.QHeaderView.Stretch)
        except Exception:  # noqa: BLE001 - older Qt spelling
            self.markerTable.horizontalHeader().setStretchLastSection(True)
        for column in (1, 2, 3):
            self.markerTable.setColumnWidth(column, 66)
        self.markerTable.verticalHeader().setVisible(False)
        self.markerTable.setMinimumHeight(180)
        self.markerTable.toolTip = (
            "Plate millimetres, origin at the plate's lower-left corner. "
            "MarkerClicks points are matched to these rows by order, so click "
            "in the same order the rows are listed. z is optional.")
        marker_form.addRow(self.markerTable)

        marker_buttons = qt.QHBoxLayout()
        self.markerAddButton = qt.QPushButton("Add")
        self.markerRemoveButton = qt.QPushButton("Remove")
        self.markerApplyButton = qt.QPushButton("Apply to cube")
        self.markerAddButton.toolTip = "Append one marker row"
        self.markerRemoveButton.toolTip = "Remove the selected row"
        self.markerApplyButton.toolTip = (
            "Draw the plate in the cube and check it against camera 1")
        marker_buttons.addWidget(self.markerAddButton)
        marker_buttons.addWidget(self.markerRemoveButton)
        marker_buttons.addWidget(self.markerApplyButton)
        marker_form.addRow(marker_buttons)

        marker_file_row = qt.QHBoxLayout()
        self.markerSaveButton = qt.QPushButton("Save")
        self.markerLoadButton = qt.QPushButton("Load")
        self.markerSaveButton.toolTip = "Write marker-plate.json"
        self.markerLoadButton.toolTip = "Read marker-plate.json into the table"
        marker_file_row.addWidget(self.markerSaveButton)
        marker_file_row.addWidget(self.markerLoadButton)
        marker_form.addRow(marker_file_row)

        self.markerStatusLabel = qt.QLabel("markers: not applied")
        self.markerStatusLabel.wordWrap = True
        marker_form.addRow(self.markerStatusLabel)

        self.advancedPanel = ctk.ctkCollapsibleButton()
        self.advancedPanel.text = "Advanced"
        self.advancedPanel.collapsed = True
        form.addRow(self.advancedPanel)
        advanced = qt.QFormLayout(self.advancedPanel)

        # The old triangulation controls, kept out of the way.
        self.cameraBox = qt.QCheckBox("Camera view clicks: one per view")
        self.cameraBox.toolTip = (
            "The video keeps playing while you click. A click moves that "
            "camera's mark to the pressed pixel immediately. Ctrl+drag pans "
            "a camera view or rotates the 3D view without placing a mark. "
            "Use a fixed target; Pause stream if the target, a camera, or an "
            "occluder can move.")
        advanced.addRow(self.cameraBox)

        self.cameraLabel = qt.QLabel("no camera views loaded")
        self.cameraLabel.wordWrap = True
        advanced.addRow(self.cameraLabel)

        camera = qt.QHBoxLayout()
        self.clearCameraButton = qt.QPushButton("Clear camera clicks")
        self.clearCameraButton.toolTip = (
            "Clear the three clicks, the solved point, rays and predicted pixels")
        camera.addWidget(self.clearCameraButton)
        advanced.addRow(camera)

        self.rayGuideLabel = qt.QLabel("ray guide: click a camera view")
        self.rayGuideLabel.wordWrap = True
        advanced.addRow(self.rayGuideLabel)

        stream = qt.QHBoxLayout()
        self.connectStreamButton = qt.QPushButton("Connect camera stream")
        self.disconnectStreamButton = qt.QPushButton("Disconnect stream")
        stream.addWidget(self.connectStreamButton)
        stream.addWidget(self.disconnectStreamButton)
        advanced.addRow(stream)

        self.truthLabel = qt.QLabel("camera result: no measurement yet")
        self.truthLabel.wordWrap = True
        advanced.addRow(self.truthLabel)

        self.cameraPixelNote = qt.QLabel("")
        self.cameraPixelNote.wordWrap = True
        advanced.addRow(self.cameraPixelNote)

        self.pickBox = qt.QCheckBox("Pick targets (clicks in Red/Green/Yellow)")
        advanced.addRow(self.pickBox)

        self.probeBox = qt.QCheckBox(
            "Probe mode (click a slice to move the camera-check point)"
        )
        advanced.addRow(self.probeBox)

        self.probeLabel = qt.QLabel("no probe yet")
        self.probeLabel.wordWrap = True
        advanced.addRow(self.probeLabel)

        self.lastLabel = qt.QLabel("no target yet")
        advanced.addRow(self.lastLabel)

        self.countsLabel = qt.QLabel("Placed: 0     Rejected: 0")
        advanced.addRow(self.countsLabel)

        targets = qt.QHBoxLayout()
        self.clearButton = qt.QPushButton("Clear targets")
        targets.addWidget(self.clearButton)
        advanced.addRow(targets)

        external = qt.QHBoxLayout()
        self.loadExternalButton = qt.QPushButton("Load external targets")
        external.addWidget(self.loadExternalButton)
        advanced.addRow(external)

        self.externalLabel = qt.QLabel("no camera targets loaded")
        self.externalLabel.wordWrap = True
        advanced.addRow(self.externalLabel)

        offline = qt.QHBoxLayout()
        self.loadCameraViewsButton = qt.QPushButton("Load camera views")
        offline.addWidget(self.loadCameraViewsButton)
        advanced.addRow(offline)

        self.cameraPixelEdits = {}
        for tag, widget_name in zip(CAMERA_CLICK_LABELS, CAMERA_VIEWS):
            row = qt.QHBoxLayout()
            row.addWidget(qt.QLabel(tag))
            u_edit = qt.QLineEdit()
            u_edit.setPlaceholderText("\u2014")
            u_edit.setMaximumWidth(90)
            v_edit = qt.QLineEdit()
            v_edit.setPlaceholderText("\u2014")
            v_edit.setMaximumWidth(90)
            row.addWidget(u_edit)
            row.addWidget(v_edit)
            advanced.addRow(f"{tag} pixel (u, v)", row)
            self.cameraPixelEdits[widget_name] = (u_edit, v_edit)
            u_edit.editingFinished.connect(
                lambda _w=widget_name: self.on_camera_pixel_edited(_w))
            v_edit.editingFinished.connect(
                lambda _w=widget_name: self.on_camera_pixel_edited(_w))

        spawn_row = qt.QHBoxLayout()
        self.spawnXEdit = qt.QLineEdit("0.0")
        self.spawnXEdit.setMaximumWidth(90)
        self.spawnYEdit = qt.QLineEdit("0.0")
        self.spawnYEdit.setMaximumWidth(90)
        self.spawnZEdit = qt.QLineEdit("0.0")
        self.spawnZEdit.setMaximumWidth(90)
        self.spawnButton = qt.QPushButton("Spawn")
        spawn_row.addWidget(qt.QLabel("x"))
        spawn_row.addWidget(self.spawnXEdit)
        spawn_row.addWidget(qt.QLabel("y"))
        spawn_row.addWidget(self.spawnYEdit)
        spawn_row.addWidget(qt.QLabel("z"))
        spawn_row.addWidget(self.spawnZEdit)
        spawn_row.addWidget(self.spawnButton)
        advanced.addRow("Spawn 3D point (rig mm)", spawn_row)

        self.spawnNote = qt.QLabel("")
        self.spawnNote.wordWrap = True
        advanced.addRow(self.spawnNote)

        self.pickBox.toggled.connect(self.onPickToggled)
        self.probeBox.toggled.connect(self.onProbeToggled)
        self.cameraBox.toggled.connect(self.onCameraToggled)
        self.clearButton.clicked.connect(self.onClear)
        self.openLogButton.clicked.connect(self.onOpenLog)
        self.loadExternalButton.clicked.connect(self.onLoadExternal)
        self.loadCameraViewsButton.clicked.connect(
            lambda _checked=False: self.onLoadCameraViews())
        self.clearCameraButton.clicked.connect(self.onClearCameraClicks)
        self.realCameraButton.clicked.connect(self.onConnectRealCameras)
        self.realCameraStopButton.clicked.connect(self.onStopRealCameras)
        self.connectStreamButton.clicked.connect(
            lambda _checked=False: self.onConnectCameraStream())
        self.disconnectStreamButton.clicked.connect(
            lambda _checked=False: self.onDisconnectCameraStream())
        self.pauseStreamButton.clicked.connect(
            lambda _checked=False: self.onPauseCameraStream())
        self.stepStreamButton.clicked.connect(
            lambda _checked=False: self.onStepCameraStream())
        self.resumeStreamButton.clicked.connect(
            lambda _checked=False: self.onResumeCameraStream())
        self.spawnButton.clicked.connect(self.on_spawn)
        self.markerAddButton.clicked.connect(self.onMarkerAdd)
        self.markerRemoveButton.clicked.connect(self.onMarkerRemove)
        self.markerApplyButton.clicked.connect(self.onMarkerApply)
        self.markerSaveButton.clicked.connect(self.onMarkerSave)
        self.markerLoadButton.clicked.connect(self.onMarkerLoad)

        self.layout.addStretch(1)

    # -- lifecycle ----------------------------------------------------------

    def enter(self):
        self.install_filters()
        self.refresh_status()
        # First entry fills the table; returning to the module must not throw
        # away edits the operator has not saved yet.
        if self.markerTable.rowCount == 0:
            self._load_marker_panel()
        try:
            status = self.logic.camera_stream_status()
        except Exception:
            status = {"status": "off", "message": "camera stream: off"}
        self.streamLabel.text = status["message"]
        try:
            self.truthLabel.text = self.logic.stream_result_status()["message"]
        except Exception:
            self.truthLabel.text = "camera result: no measurement yet"
        try:
            video = self.logic.video_status()
        except Exception:
            video = {"status": "off", "message": "video: off"}
        self.videoLabel.text = video["message"]
        self.realCameraStopButton.setEnabled(video["status"] == "running")
        if status["status"] in ("waiting", "connected", "disconnected"):
            self._watch_stream_label()

    def exit(self):
        self._stop_video_quietly()
        self.logic.stop_watch()
        self._stop_stream_timer()
        self.set_pick_enabled(False)
        self.remove_filters()

    def cleanup(self):
        self._stop_video_quietly()
        self._stop_stream_timer()
        self.remove_filters()

    def _stop_video_quietly(self):
        try:
            result = self.logic.stop_video()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            self.videoLabel.text = f"video: could not stop: {exc}"
            return
        self.videoLabel.text = result["message"]
        self.realCameraStopButton.setEnabled(False)

    def _mouse_target(self, view_widget):
        """The widget that actually receives mouse events in a slice view.

        qMRMLSliceView is a parent; Qt delivers the press to its
        ctkVTKOpenGLNativeWidget child first. A filter on the parent sees the
        press during propagation, after the VTK interactor has already handled
        it, so returning True cannot stop a markups drag. On the child the
        filter runs before the interactor and a press can be consumed, which
        is what "a press moves the point to that location" needs.
        """
        for child in view_widget.children():
            if "ctkVTKOpenGLNativeWidget" in type(child).__name__:
                return child
        return view_widget

    def install_filters(self):
        if self.filters:
            return
        layout = slicer.app.layoutManager()
        for view, widget_name in VIEW_WIDGETS.items():
            slice_widget = layout.sliceWidget(widget_name)
            if slice_widget is None:
                continue
            target = self._mouse_target(slice_widget.sliceView())
            view_filter = SliceClickFilter(view, self.on_click)
            target.installEventFilter(view_filter)
            self.filters[widget_name] = (target, view_filter)
        three_d = layout.threeDWidget(0)
        if three_d is not None:
            three_view = three_d.threeDView()
            target = self._mouse_target(three_view)
            three_filter = ThreeDViewClickFilter(three_view)
            target.installEventFilter(three_filter)
            self.threeDFilter = (target, three_filter)

    def remove_filters(self):
        for view_widget, view_filter in self.filters.values():
            view_widget.removeEventFilter(view_filter)
        self.filters = {}
        if self.threeDFilter is not None:
            target, three_filter = self.threeDFilter
            target.removeEventFilter(three_filter)
            self.threeDFilter = None
        self.pickBox.checked = False  # toggled() flips the enabled flags
        self.probeBox.checked = False
        self.cameraBox.checked = False

    def set_pick_enabled(self, enabled):
        for _widget, view_filter in self.filters.values():
            view_filter.click_enabled = bool(enabled)

    # -- handlers -------------------------------------------------------------

    def refresh_filters(self):
        """Clicks are wanted when any mode is on; all modes share the filters."""
        self.set_pick_enabled(self.pickBox.checked or self.probeBox.checked
                              or self.cameraBox.checked)

    def _exclusive_mode(self, mode):
        """Release the other click modes, so the last tick is the only one."""
        for box in (self.pickBox, self.probeBox, self.cameraBox):
            if box is not mode and box.checked:
                box.checked = False

    def onPickToggled(self, checked):
        if checked:
            self._exclusive_mode(self.pickBox)
        self.refresh_filters()

    def onCameraToggled(self, checked):
        if checked:
            self._exclusive_mode(self.cameraBox)
        self.refresh_filters()
        if checked:
            self.cameraLabel.text = (
                "camera mode: live video (fixed target); click the SAME point "
                "once in Red, Green and Yellow; each click moves that mark "
                "at once")
            self.rayGuideLabel.text = self.logic.ray_guide_status()
            try:
                timer = self.logic.watch_camera_clicks()
                try:
                    timer.timeout.disconnect(self._refresh_camera_label)
                except Exception:
                    pass
                timer.timeout.connect(self._refresh_camera_label)
            except Exception:
                pass
        else:
            self.logic.clear_camera_ray_guide()
            self.rayGuideLabel.text = "ray guide: off"

    def _refresh_camera_label(self):
        try:
            self.cameraLabel.text = self.logic.camera_status()
        except Exception:
            pass
        self._refresh_camera_pixel_fields()
        self._update_identical_warning()

    def onProbeToggled(self, checked):
        if checked:
            self._exclusive_mode(self.probeBox)
        self.refresh_filters()
        if checked:
            self.logic.watch_probe()
            self.probeLabel.text = ("probe mode: click a slice, or drag the "
                                    "SandboxProbe point, and the sandbox follows")
        else:
            self.logic.stop_probe_watch()
            self.probeLabel.text = "probe mode: off"

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

        Each click publishes the live set at once -- views not clicked yet
        go out as null -- and leaves that camera's mark on its view. A
        refused click names its view on the label instead of crashing Slicer.
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
        status = self.logic.camera_status()
        if result["published"] is not None:
            self.cameraLabel.text = (
                f"{status} — published {result['published'].name}"
            )
        else:
            self.cameraLabel.text = (
                f"{status} ({widget_name} pixel ({u:.1f}, {v:.1f}))"
            )
        self.rayGuideLabel.text = self.logic.ray_guide_status()
        self._refresh_camera_pixel_fields()
        self._update_identical_warning()

    def on_camera_pixel_edited(self, widget_name):
        """A typed pixel from the panel's c-row fields.

        Parses this camera's two fields as floats and applies them through
        apply_camera_pixel -- the same store/mark/publish tail the
        slice-widget clicks use. A blank row is not an edit (nothing to
        apply); anything unparseable is noted and the fields are restored.
        """
        pair = self.cameraPixelEdits.get(widget_name)
        if pair is None:
            return {"status": "refused", "message": f"{widget_name}: no pixel row"}
        u_edit, v_edit = pair
        u_text = u_edit.text.strip()
        v_text = v_edit.text.strip()
        if not u_text or not v_text:
            self._refresh_camera_pixel_fields()
            return {"status": "ignored", "message": "empty pixel field"}
        try:
            u, v = float(u_text), float(v_text)
        except ValueError:
            tag = CAMERA_CLICK_LABELS[CAMERA_VIEWS.index(widget_name)]
            self.cameraPixelNote.text = f"{tag} needs numbers for u and v"
            self._refresh_camera_pixel_fields()
            return {"status": "refused", "message": self.cameraPixelNote.text}
        return self.apply_camera_pixel(widget_name, u, v)

    def apply_camera_pixel(self, widget_name, u, v):
        """Apply a typed (u, v) pixel for one camera, like a click would.

        The single programmatic entry the smoke drives exactly as a real edit
        does: logic.set_camera_pixel resolves RAS from the volume's IJKToRAS
        and stores through the shared tail, so the sandbox follows via the
        existing file. Out-of-bounds values are refused -- state untouched,
        note shown, fields restored -- never clamped.
        """
        tag = CAMERA_CLICK_LABELS[CAMERA_VIEWS.index(widget_name)]
        try:
            result = self.logic.set_camera_pixel(widget_name, float(u), float(v))
        except ValueError as exc:
            self.cameraPixelNote.text = self._outside_note(widget_name, str(exc))
            self._refresh_camera_pixel_fields()
            return {"status": "refused", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a typed pixel must never crash Slicer
            import traceback  # noqa: PLC0415

            traceback.print_exc()
            self.cameraPixelNote.text = f"Internal error: {exc} (details in console)"
            self._refresh_camera_pixel_fields()
            return {"status": "refused", "message": str(exc)}
        status = self.logic.camera_status()
        if result["published"] is not None:
            self.cameraLabel.text = (
                f"{status} — published {result['published'].name}"
            )
        else:
            self.cameraLabel.text = status
        self.rayGuideLabel.text = self.logic.ray_guide_status()
        self.cameraPixelNote.text = ""
        self._refresh_camera_pixel_fields()
        self._update_identical_warning()
        return result

    def _outside_note(self, widget_name, message):
        """The refusal note for a typed out-of-picture pixel.

        Names the camera tag with its picture range, e.g.
        `c2 outside the picture (0..319)`. The range is read from the
        background volume; when the view has no picture the logic message
        (which already says "outside the picture") stands as-is.
        """
        tag = CAMERA_CLICK_LABELS[CAMERA_VIEWS.index(widget_name)]
        if "outside the picture" not in message:
            return message
        try:
            widget = slicer.app.layoutManager().sliceWidget(widget_name)
            node = slicer.mrmlScene.GetNodeByID(
                widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID())
            if node is not None and node.GetImageData() is not None:
                width = node.GetImageData().GetDimensions()[0]
                return f"{tag} outside the picture (0..{int(width) - 1})"
        except Exception:
            pass
        return f"{tag} outside the picture"

    def _refresh_camera_pixel_fields(self):
        """Show each camera's published pixel, without fighting the typist.

        A field the user is currently editing (keyboard focus) is left alone
        so programmatic refreshes never overwrite mid-typing; every other
        field follows the live click state. A view with no click shows the
        placeholder (empty text).
        """
        edits = getattr(self, "cameraPixelEdits", None)
        if not edits:
            return
        for widget_name, pair in edits.items():
            u_edit, v_edit = pair
            try:
                pixel = self.logic.clicks.get(widget_name)
            except Exception:
                continue
            if pixel is None:
                try:
                    if not u_edit.hasFocus():
                        u_edit.clear()
                    if not v_edit.hasFocus():
                        v_edit.clear()
                except Exception:
                    pass
            else:
                try:
                    if not u_edit.hasFocus():
                        u_edit.setText(f"{pixel[0]:.1f}")
                    if not v_edit.hasFocus():
                        v_edit.setText(f"{pixel[1]:.1f}")
                except Exception:
                    pass

    def _update_identical_warning(self):
        """Warn when all three pixels agree to the pixel -- today's mistake.

        One 3D object never projects to the same pixel in three different
        cameras; typing the same numbers into all three rows collapses the
        rays. Every pair within 0.5 px raises the warning (stable substring
        `identical`); any other state clears it.
        """
        note = getattr(self, "cameraPixelNote", None)
        if note is None:
            return
        try:
            pixels = [self.logic.clicks.get(w) for w in CAMERA_VIEWS]
        except Exception:
            return
        if any(p is None for p in pixels):
            if "identical" in note.text:
                note.text = ""
            return
        worst = 0.0
        for i in range(len(pixels)):
            for j in range(i + 1, len(pixels)):
                du = pixels[i][0] - pixels[j][0]
                dv = pixels[i][1] - pixels[j][1]
                worst = max(worst, (du * du + dv * dv) ** 0.5)
        if worst <= 0.5:
            note.text = ("warning: all three pixels identical — three cameras "
                         "rarely agree to the pixel, check the marks")
        elif "identical" in note.text:
            note.text = ""

    def on_spawn(self):
        """Seed the three camera clicks from the typed rig-mm 3D point.

        The file's matrices say where the point lands in each picture, and
        those pixels are applied through the click state in one pass -- so
        afterwards the marks are ordinary editable points. An out-of-picture
        camera becomes an unclicked slot. Garbage or a missing rig file
        refuses with a note and changes nothing (the pixel rows' ValueError
        pattern).
        """
        try:
            x = float(self.spawnXEdit.text.strip())
            y = float(self.spawnYEdit.text.strip())
            z = float(self.spawnZEdit.text.strip())
        except ValueError:
            self.spawnNote.text = "spawn needs numbers for x, y and z"
            return {"status": "refused", "message": self.spawnNote.text}
        try:
            result = self.logic.spawn_point(x, y, z)
        except ValueError as exc:
            self.spawnNote.text = f"spawn needs numbers for x, y and z ({exc})"
            return {"status": "refused", "message": self.spawnNote.text}
        except Exception as exc:  # noqa: BLE001 - a spawn must never crash Slicer
            import traceback  # noqa: PLC0415

            traceback.print_exc()
            self.spawnNote.text = f"Internal error: {exc} (details in console)"
            return {"status": "refused", "message": str(exc)}
        if result.get("status") == "refused":
            self.spawnNote.text = str(result.get("message", "spawn refused"))
            return result
        parts = []
        for tag, pixel in zip(CAMERA_CLICK_LABELS, result["pixels"]):
            if pixel is None:
                parts.append(f"{tag} outside the picture")
            else:
                parts.append(f"{tag} ({pixel[0]:.1f}, {pixel[1]:.1f})")
        self.spawnNote.text = (f"spawned ({x:g}, {y:g}, {z:g}): "
                               + " ".join(parts))
        self.cameraLabel.text = self.logic.camera_status()
        self._refresh_camera_pixel_fields()
        self._update_identical_warning()
        return result

    def onClearCameraClicks(self, checked=False):
        self.logic.clear_camera_clicks()
        self.cameraLabel.text = "camera clicks cleared (0 of 3)"
        self.rayGuideLabel.text = self.logic.ray_guide_status()
        self.truthLabel.text = self.logic.stream_result_status()["message"]
        self.cameraPixelNote.text = ""
        self._refresh_camera_pixel_fields()

    # -- the marker plate -------------------------------------------------

    def _load_marker_panel(self):
        """Fill the table from marker-plate.json, quietly when it is absent."""
        from targeting.marker_plate import load_plate

        try:
            plate = load_plate()
        except RuntimeError as exc:
            self.markerStatusLabel.text = f"markers: {exc}"
            return
        self._fill_marker_table(plate)
        self.markerStatusLabel.text = (
            f"markers: {len(plate['markers'])} loaded from {plate['path']} "
            f"(not applied)")

    def _fill_marker_table(self, plate):
        """Show a validated plate in the panel fields."""
        self.markerTable.setRowCount(0)
        for marker in plate["markers"]:
            row = self.markerTable.rowCount
            self.markerTable.insertRow(row)
            self.markerTable.setItem(row, 0, qt.QTableWidgetItem(marker["label"]))
            for column, value in ((1, marker["mm"][0]),
                                  (2, marker["mm"][1]),
                                  (3, marker["z"])):
                self.markerTable.setItem(
                    row, column, qt.QTableWidgetItem(f"{value:g}"))
        self.markerWidthSpin.value = plate["width"]
        self.markerHeightSpin.value = plate["height"]
        (self.markerOriginXSpin.value, self.markerOriginYSpin.value,
         self.markerOriginZSpin.value) = plate["origin"]

    def _plate_from_panel(self):
        """The panel as a validated plate, refusing by row and column."""
        from targeting.marker_plate import validate_plate

        markers = []
        for row in range(self.markerTable.rowCount):
            label_item = self.markerTable.item(row, 0)
            label = label_item.text().strip() if label_item is not None else ""
            if not label:
                raise RuntimeError(f"row {row + 1} has no label")
            values = []
            for column, name in ((1, "x"), (2, "y"), (3, "z")):
                item = self.markerTable.item(row, column)
                text = item.text().strip() if item is not None else ""
                if not text and name == "z":
                    text = "0"
                if not text:
                    raise RuntimeError(f"{label} has no {name}")
                try:
                    values.append(float(text))
                except ValueError:
                    raise RuntimeError(
                        f"{label}: {name} {text!r} is not a number") from None
            markers.append({"label": label, "mm": values})
        data = {
            "plate_mm": {"width": self.markerWidthSpin.value,
                         "height": self.markerHeightSpin.value},
            "markers": markers,
            "cube_origin_mm": [self.markerOriginXSpin.value,
                               self.markerOriginYSpin.value,
                               self.markerOriginZSpin.value],
        }
        return validate_plate(data, "the panel")

    def onMarkerAdd(self, checked=False):
        row = self.markerTable.rowCount
        self.markerTable.insertRow(row)
        self.markerTable.setItem(row, 0, qt.QTableWidgetItem(f"M{row + 1}"))
        for column, value in ((1, "0"), (2, "0"), (3, "0")):
            self.markerTable.setItem(row, column, qt.QTableWidgetItem(value))

    def onMarkerRemove(self, checked=False):
        row = self.markerTable.currentRow
        if row < 0:
            row = self.markerTable.rowCount - 1
        if row >= 0:
            self.markerTable.removeRow(row)

    def onMarkerApply(self, checked=False):
        """Draw the plate in the cube and check it against the live picture."""
        from TargetingLib import marker_plate as scene

        try:
            plate = self._plate_from_panel()
        except RuntimeError as exc:
            self.markerStatusLabel.text = f"markers: {exc}"
            return
        try:
            result = scene.apply(plate)
        except Exception as exc:  # noqa: BLE001 - shown to the operator
            self.markerStatusLabel.text = f"markers: {exc}"
            return
        self.markerStatusLabel.text = result["text"]

    def onMarkerSave(self, checked=False):
        from targeting.marker_plate import save_plate

        try:
            plate = self._plate_from_panel()
            path = save_plate(plate)
        except RuntimeError as exc:
            self.markerStatusLabel.text = f"markers: {exc}"
            return
        self.markerStatusLabel.text = f"markers saved to {path}"

    def onMarkerLoad(self, checked=False):
        self._load_marker_panel()

    # -- the live camera stream ------------------------------------------

    def onConnectRealCameras(self):
        """One button: start the sender if needed, then connect the views."""
        device = self.realCameraDeviceEdit.text.strip() or "/dev/video0"
        try:
            connected = self.logic.connect_camera_stream(
                port=int(self.portSpin.value))
        except Exception as exc:            # noqa: BLE001 - shown in the panel
            self.streamLabel.text = f"camera stream: could not connect: {exc}"
            return
        self.streamLabel.text = connected["message"]
        self._watch_stream_label()

        try:
            result = self.logic.start_real_cameras(
                device=device, fps=float(self.fpsSpin.value))
        except Exception as exc:            # noqa: BLE001 - shown in the panel
            self.videoLabel.text = f"cameras: could not start: {exc}"
            return
        self.videoLabel.text = result["message"]
        self.realCameraStopButton.setEnabled(result["status"] == "running")
        if result["status"] != "running":
            self.videoNote.text = result["message"]

    def onStopRealCameras(self):
        try:
            result = self.logic.stop_video()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.videoLabel.text = f"cameras: could not stop: {exc}"
            return
        self.videoLabel.text = result["message"]
        self.realCameraStopButton.setEnabled(False)
        try:
            stream = self.logic.disconnect_camera_stream()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.streamLabel.text = f"camera stream: could not disconnect: {exc}"
            return
        self.streamLabel.text = stream["message"]
        self._stop_stream_timer()

    def onConnectCameraStream(self):
        try:
            result = self.logic.connect_camera_stream()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.streamLabel.text = f"camera stream: could not connect: {exc}"
            return
        self.streamLabel.text = result["message"]
        self._watch_stream_label()

    def onDisconnectCameraStream(self):
        try:
            result = self.logic.disconnect_camera_stream()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.streamLabel.text = f"camera stream: could not disconnect: {exc}"
            return
        self.streamLabel.text = result["message"]
        try:
            video = self.logic.video_status()
        except Exception:
            video = {"message": "video: stopped"}
        self.videoLabel.text = video["message"]
        self.realCameraStopButton.setEnabled(False)
        self._stop_stream_timer()

    def onPauseCameraStream(self):
        try:
            result = self.logic.pause_camera_stream()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.streamLabel.text = f"camera stream: could not pause: {exc}"
            return
        self.streamLabel.text = result["message"]
        self._watch_stream_label()

    def onStepCameraStream(self):
        try:
            result = self.logic.step_camera_stream()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.streamLabel.text = f"camera stream: could not step: {exc}"
            return
        self.streamLabel.text = result["message"]
        self._watch_stream_label()

    def onResumeCameraStream(self):
        try:
            result = self.logic.resume_camera_stream()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.streamLabel.text = f"camera stream: could not resume: {exc}"
            return
        self.streamLabel.text = result["message"]
        self._watch_stream_label()

    def _watch_stream_label(self):
        if self.streamTimer is None:
            self.streamTimer = qt.QTimer()
            self.streamTimer.setInterval(500)
            self.streamTimer.timeout.connect(self._refresh_stream_label)
        self.streamTimer.start()

    def _stop_stream_timer(self):
        if self.streamTimer is not None:
            self.streamTimer.stop()

    def _refresh_stream_label(self):
        try:
            result = self.logic.camera_stream_status()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.streamLabel.text = f"camera stream: {exc}"
            self._stop_stream_timer()
            return
        self.streamLabel.text = result["message"]
        try:
            self.truthLabel.text = self.logic.stream_result_status()["message"]
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.truthLabel.text = f"camera result: {exc}"
        try:
            self.rayGuideLabel.text = self.logic.ray_guide_status()
        except Exception:
            pass
        video = None
        try:
            video = self.logic.video_status()
        except Exception as exc:            # noqa: BLE001 - shown to the operator
            self.videoLabel.text = f"video: {exc}"
        else:
            self.videoLabel.text = video["message"]
            self.realCameraStopButton.setEnabled(video["status"] == "running")
        if (result["status"] in ("off", "unavailable", "error", "paused")
                and not (video and video["status"] == "running")):
            self._stop_stream_timer()

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

    def onLoadCameraViews(self, folder=None):
        """Load the three camera pictures, asking which folder first.

        The chooser starts at the last loaded folder, or the module default
        `logs/camera-frames`. A scripted caller (the smoke) may pass a folder
        to skip the dialog; cancelling changes nothing in the scene.
        """
        if folder is None:
            start = getattr(self.logic, "frames_folder", None)
            start = str(start if start is not None else LOGS_DIR / "camera-frames")
            chosen = qt.QFileDialog.getExistingDirectory(
                slicer.util.mainWindow(),
                "Choose the frames folder (camera-frames.json + PNGs)",
                start)
            if not chosen:
                self.cameraLabel.text = "load cancelled — camera views unchanged"
                return
            folder = chosen
        try:
            result = self.logic.load_camera_views(folder)
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
        self.statusLabel.text = (
            "Connect the real cameras, then declare the marker plate below. "
            "Apply draws it in the cube and checks it against camera 1.")
