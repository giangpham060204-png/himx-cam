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
        self.cameraFilters = {}
        self.lastClick = None
        self.lastCube = None
        self.lastReal = None
        self.registrationFit = None
        self.streamTimer = None
        self.threeDFilter = None
        self._video_demo_mode = False
        self._layout_busy = False

    # -- UI ---------------------------------------------------------------

    def setup(self):
        super().setup()

        panel = ctk.ctkCollapsibleButton()
        panel.text = "Targeting"
        self.layout.addWidget(panel)
        form = qt.QFormLayout(panel)

        self.statusLabel = qt.QLabel(
            "Tick Pick targets, then click a point in Red/Green/Yellow. The "
            "slice slider supplies the third coordinate. Fit the virtual -> "
            "real markers, then Map last target.")
        self.statusLabel.wordWrap = True
        form.addRow(self.statusLabel)

        # The virtual -> real result lives at the top, not inside the
        # registration section: it must stay visible when the panel is tall
        # or the registration section is scrolled.
        self.realTargetLabel = qt.QLabel(
            "real target: none — fit the markers, place a target, then Map")
        self.realTargetLabel.wordWrap = True
        self.realTargetLabel.setStyleSheet("font-weight: bold;")
        form.addRow(self.realTargetLabel)

        # Pick mode is the main workflow, not an Advanced option: a click in a
        # standard slice places the virtual target that Map sends to the real
        # frame. The slice slider is still the third coordinate.
        self.pickBox = qt.QCheckBox(
            "Pick targets (click Red/Green/Yellow)")
        self.pickBox.toolTip = (
            "When ticked, a click in Red, Green or Yellow places a target at "
            "the clicked pixel and the slice slider's depth. The 3D view is "
            "for viewing, not placing.")
        form.addRow(self.pickBox)

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

        self.cameraLayoutButton = qt.QPushButton(
            "Camera layout (feeds + four-up)")
        self.cameraLayoutButton.setCheckable(True)
        self.cameraLayoutButton.toolTip = (
            "Three camera panes in a left column, with the standard four-up "
            "(Red/Green/Yellow + 3D) beside them. The feeds get their own "
            "views (c1, c2, c3), so a camera picture is never mistaken for a "
            "coordinate space, and the slice views keep working for points.")
        form.addRow(self.cameraLayoutButton)

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
        self.markerPanel.text = "Marker plate (camera check)"
        # The virtual -> real registration is the client's workflow now; the
        # plate panel stays available but collapsed so its table cannot push
        # the registration and the mapped-target readout off the panel.
        self.markerPanel.collapsed = True
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
        self.markerTable.setMaximumHeight(200)
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

        # -- virtual -> real --------------------------------------------
        # The client's flow: pick in the virtual scene, map through measured
        # markers to the real frame. The camera is not in this coordinate
        # path; it can verify the markers, but the transform is rigid.
        self.registrationPanel = ctk.ctkCollapsibleButton()
        self.registrationPanel.text = "Virtual → real (measured markers)"
        self.registrationPanel.collapsed = False
        form.addRow(self.registrationPanel)
        registration_form = qt.QFormLayout(self.registrationPanel)

        self.realFrameEdit = qt.QLineEdit("arm-base")
        self.realFrameEdit.toolTip = (
            "Name of the measured frame. The r* numbers below are in it; the "
            "name is recorded in every mapped target.")
        registration_form.addRow("real frame", self.realFrameEdit)

        self.registrationHint = qt.QLabel(
            "V* = marker position in the Slicer cube (mm). R* = the same "
            "marker measured in the real frame (mm). These are the "
            "registration inputs, not the picked target; the mapped target "
            "appears at the top of the panel after Map. Double-click a cell "
            "to edit it.")
        self.registrationHint.wordWrap = True
        registration_form.addRow(self.registrationHint)

        self.registrationTable = qt.QTableWidget(0, 7)
        self.registrationTable.setHorizontalHeaderLabels(
            ["marker", "Vx", "Vy", "Vz", "Rx", "Ry", "Rz"])
        try:
            self.registrationTable.horizontalHeader().setSectionResizeMode(
                0, qt.QHeaderView.Stretch)
        except Exception:  # noqa: BLE001 - older Qt spelling
            self.registrationTable.horizontalHeader().setStretchLastSection(
                True)
        for column in range(1, 7):
            self.registrationTable.setColumnWidth(column, 52)
        self.registrationTable.verticalHeader().setVisible(False)
        self.registrationTable.setMinimumHeight(120)
        self.registrationTable.setMaximumHeight(150)
        self.registrationTable.toolTip = (
            "v* = virtual Slicer cube millimetres; r* = the same marker "
            "measured in the real frame. Three non-collinear markers are the "
            "minimum; four or more make the residual a stronger check.")
        registration_form.addRow(self.registrationTable)

        registration_buttons = qt.QHBoxLayout()
        self.registrationSyncButton = qt.QPushButton("Sync virtual from plate")
        self.registrationFitButton = qt.QPushButton("Fit")
        self.registrationMapButton = qt.QPushButton("Map last target")
        self.registrationSyncButton.toolTip = (
            "Copy each plate marker's cube coordinates into the v* columns, "
            "matching by label; the r* measurements are kept")
        self.registrationFitButton.toolTip = (
            "Fit the rigid virtual-to-real transform and show the residual")
        self.registrationMapButton.toolTip = (
            "Map the last placed target from the virtual cube to the real "
            "frame")
        registration_buttons.addWidget(self.registrationSyncButton)
        registration_buttons.addWidget(self.registrationFitButton)
        registration_buttons.addWidget(self.registrationMapButton)
        registration_form.addRow(registration_buttons)

        registration_edit_row = qt.QHBoxLayout()
        self.registrationAddButton = qt.QPushButton("Add marker")
        self.registrationRemoveButton = qt.QPushButton("Remove")
        self.registrationAddButton.toolTip = "Append one marker row"
        self.registrationRemoveButton.toolTip = "Remove the selected row"
        registration_edit_row.addWidget(self.registrationAddButton)
        registration_edit_row.addWidget(self.registrationRemoveButton)
        registration_form.addRow(registration_edit_row)

        registration_file_row = qt.QHBoxLayout()
        self.registrationSaveButton = qt.QPushButton("Save")
        self.registrationLoadButton = qt.QPushButton("Load")
        self.registrationSaveButton.toolTip = "Write marker-registration.json"
        self.registrationLoadButton.toolTip = (
            "Read marker-registration.json into the table")
        registration_file_row.addWidget(self.registrationSaveButton)
        registration_file_row.addWidget(self.registrationLoadButton)
        registration_form.addRow(registration_file_row)

        self.registrationStatusLabel = qt.QLabel("registration: not fitted")
        self.registrationStatusLabel.wordWrap = True
        registration_form.addRow(self.registrationStatusLabel)

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

        # Pick targets lives in the main panel now; the probe and camera
        # modes remain here as the legacy/advanced paths.
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
        self.cameraLayoutButton.toggled.connect(self.onCameraLayoutToggled)
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
        self.registrationSyncButton.clicked.connect(self.onRegistrationSync)
        self.registrationFitButton.clicked.connect(self.onRegistrationFit)
        self.registrationMapButton.clicked.connect(self.onRegistrationMap)
        self.registrationSaveButton.clicked.connect(self.onRegistrationSave)
        self.registrationLoadButton.clicked.connect(self.onRegistrationLoad)
        self.registrationAddButton.clicked.connect(self.onRegistrationAdd)
        self.registrationRemoveButton.clicked.connect(self.onRegistrationRemove)
        self.registrationTable.itemChanged.connect(self.onRegistrationEdited)
        self.realFrameEdit.textChanged.connect(self.onRegistrationEdited)

        self.layout.addStretch(1)

    # -- lifecycle ----------------------------------------------------------

    def enter(self):
        self.install_filters()
        self.refresh_status()
        self._sync_layout_button()
        # First entry fills the table; returning to the module must not throw
        # away edits the operator has not saved yet.
        if self.markerTable.rowCount == 0:
            self._load_marker_panel()
        if self.registrationTable.rowCount == 0:
            self._load_registration_panel()
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
        """Attach the click filters to every view that is showing.

        Called again after a layout switch: a camera view only exists once
        the camera layout has been shown. Each name is checked before it is
        added, so a second call never stacks two filters on one widget.
        """
        layout = slicer.app.layoutManager()
        for view, widget_name in VIEW_WIDGETS.items():
            if widget_name in self.filters:
                continue
            slice_widget = layout.sliceWidget(widget_name)
            if slice_widget is None:
                continue
            target = self._mouse_target(slice_widget.sliceView())
            view_filter = SliceClickFilter(view, self.on_click)
            target.installEventFilter(view_filter)
            self.filters[widget_name] = (target, view_filter)

        # Camera panes get their own filters, addressed by the pane name: a
        # click there is a pixel in that camera, never a slice-mode click.
        for widget_name in CAMERA_VIEWS:
            if widget_name in self.cameraFilters:
                continue
            slice_widget = layout.sliceWidget(widget_name)
            if slice_widget is None:
                continue
            target = self._mouse_target(slice_widget.sliceView())
            view_filter = SliceClickFilter(widget_name, self.on_camera_click)
            target.installEventFilter(view_filter)
            self.cameraFilters[widget_name] = (target, view_filter)

        if self.threeDFilter is None:
            three_d = layout.threeDWidget(0)
            if three_d is not None:
                three_view = three_d.threeDView()
                target = self._mouse_target(three_view)
                three_filter = ThreeDViewClickFilter(three_view)
                target.installEventFilter(three_filter)
                self.threeDFilter = (target, three_filter)

    def remove_filters(self, reset_modes=True):
        for target, view_filter in (list(self.filters.values())
                                    + list(self.cameraFilters.values())):
            try:
                target.removeEventFilter(view_filter)
            except RuntimeError:
                # The layout switch already deleted the widget underneath it.
                pass
        self.filters = {}
        self.cameraFilters = {}
        if self.threeDFilter is not None:
            target, three_filter = self.threeDFilter
            try:
                target.removeEventFilter(three_filter)
            except RuntimeError:
                pass
            self.threeDFilter = None
        if reset_modes:
            self.pickBox.checked = False  # toggled() flips the enabled flags
            self.probeBox.checked = False
            self.cameraBox.checked = False

    def set_pick_enabled(self, enabled):
        for _widget, view_filter in self.filters.values():
            view_filter.click_enabled = bool(enabled)

    def set_camera_enabled(self, enabled):
        for _widget, view_filter in self.cameraFilters.values():
            view_filter.click_enabled = bool(enabled)

    # -- handlers -------------------------------------------------------------

    def refresh_filters(self):
        """Clicks follow the ticked mode: camera panes only in camera mode."""
        self.set_pick_enabled(self.pickBox.checked or self.probeBox.checked)
        self.set_camera_enabled(self.cameraBox.checked)

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
                "once in c1, c2 and c3; each click moves that mark at once")
            if not self.cameraFilters:
                self.cameraLabel.text += (
                    " — press Camera layout to show the camera panes")
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
            self.lastCube = tuple(float(value) for value in cube)
            self.lastReal = None
            self.realTargetLabel.text = (
                "mapped target: none — press Map for this target")
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

    def on_camera_click(self, widget_name, u_px, v_px):
        """A click in a camera pane becomes that camera's image pixel.

        Each click publishes the live set at once -- views not clicked yet
        go out as null -- and leaves that camera's mark on its view. A
        refused click names its view on the label instead of crashing Slicer.
        """
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

    # -- the virtual -> real registration --------------------------------

    def onRegistrationEdited(self, *_args):
        """Any edit invalidates the fitted transform until Fit is pressed."""
        if self.registrationFit is not None:
            self.registrationFit = None
            self.registrationStatusLabel.text = (
                "registration: edited; fit the markers again")
            self.realTargetLabel.text = (
                "mapped target: none (registration edited)")

    def _fill_registration_table(self, registration):
        """Show a loaded registration in the panel fields."""
        self.registrationTable.setRowCount(0)
        for marker in registration["markers"]:
            row = self.registrationTable.rowCount
            self.registrationTable.insertRow(row)
            self.registrationTable.setItem(
                row, 0, qt.QTableWidgetItem(marker["label"]))
            for column, value in enumerate(
                    (*marker["virtual_mm"], *marker["real_mm"]), start=1):
                self.registrationTable.setItem(
                    row, column, qt.QTableWidgetItem(f"{value:g}"))
        self.realFrameEdit.text = registration["real_frame"]

    def _registration_from_panel(self):
        """The panel as a validated registration, refusing by row and column."""
        from targeting.registration import validate_registration

        markers = []
        for row in range(self.registrationTable.rowCount):
            label_item = self.registrationTable.item(row, 0)
            label = label_item.text().strip() if label_item is not None else ""
            if not label:
                raise RuntimeError(f"row {row + 1} has no label")
            values = []
            for column, name in ((1, "virtual x"), (2, "virtual y"),
                                 (3, "virtual z"), (4, "real x"),
                                 (5, "real y"), (6, "real z")):
                item = self.registrationTable.item(row, column)
                text = item.text().strip() if item is not None else ""
                if not text and name.endswith("z"):
                    text = "0"
                if not text:
                    raise RuntimeError(f"{label} has no {name}")
                try:
                    values.append(float(text))
                except ValueError:
                    raise RuntimeError(
                        f"{label}: {name} {text!r} is not a number") from None
            markers.append({"label": label,
                            "virtual_mm": values[:3],
                            "real_mm": values[3:]})
        return validate_registration(
            {"real_frame": self.realFrameEdit.text.strip(),
             "markers": markers}, "the panel")

    def _load_registration_panel(self):
        """Fill the table from marker-registration.json, quietly when absent."""
        from targeting import registration

        try:
            config = registration.load_registration(registration.DEFAULT_PATH)
        except RuntimeError as exc:
            self.registrationStatusLabel.text = f"registration: {exc}"
            return
        self._fill_registration_table(config)
        if config["fittable"]:
            self.registrationStatusLabel.text = (
                f"registration: {len(config['markers'])} markers loaded from "
                f"{config['path']} (not fitted)")
        else:
            self.registrationStatusLabel.text = (
                f"registration: {config['note']}; fill the measured mm")
        self.realTargetLabel.text = "mapped target: none (registration loaded)"

    def onRegistrationLoad(self, checked=False):
        self._load_registration_panel()

    def onRegistrationAdd(self, checked=False):
        row = self.registrationTable.rowCount
        self.registrationTable.insertRow(row)
        self.registrationTable.setItem(
            row, 0, qt.QTableWidgetItem(f"M{row + 1}"))
        for column in range(1, 7):
            self.registrationTable.setItem(row, column,
                                           qt.QTableWidgetItem("0"))

    def onRegistrationRemove(self, checked=False):
        row = self.registrationTable.currentRow
        if row < 0:
            row = self.registrationTable.rowCount - 1
        if row >= 0:
            self.registrationTable.removeRow(row)

    def onRegistrationSync(self, checked=False):
        """Copy the plate's cube coordinates into the v* columns by label."""
        try:
            plate = self._plate_from_panel()
        except RuntimeError as exc:
            self.registrationStatusLabel.text = f"registration: {exc}"
            return

        rows = {}
        for row in range(self.registrationTable.rowCount):
            item = self.registrationTable.item(row, 0)
            if item is not None and item.text().strip():
                rows[item.text().strip()] = row

        origin = plate["origin"]
        for marker in plate["markers"]:
            label = marker["label"]
            virtual = (origin[0] + marker["mm"][0],
                       origin[1] + marker["mm"][1],
                       origin[2] + marker["z"])
            row = rows.get(label)
            if row is None:
                row = self.registrationTable.rowCount
                self.registrationTable.insertRow(row)
                self.registrationTable.setItem(
                    row, 0, qt.QTableWidgetItem(label))
                for column in range(4, 7):
                    self.registrationTable.setItem(
                        row, column, qt.QTableWidgetItem("0"))
                rows[label] = row
            for column, value in enumerate(virtual, start=1):
                self.registrationTable.setItem(
                    row, column, qt.QTableWidgetItem(f"{value:g}"))
        self.registrationStatusLabel.text = (
            "registration: virtual coordinates synced from the plate; the "
            "measured real mm are unchanged")

    def onRegistrationFit(self, checked=False):
        """Fit the rigid transform and write the solved map beside the logs."""
        from datetime import datetime

        from targeting import registration

        try:
            config = self._registration_from_panel()
        except RuntimeError as exc:
            self.registrationStatusLabel.text = f"registration: {exc}"
            self.realTargetLabel.text = (
                "mapped target: none (registration invalid)")
            return
        if not config["fittable"]:
            self.registrationFit = None
            self.registrationStatusLabel.text = f"registration: {config['note']}"
            self.realTargetLabel.text = (
                "mapped target: none (registration not fittable)")
            return
        try:
            fit = registration.fit_registration(config)
        except (RuntimeError, ValueError) as exc:
            self.registrationFit = None
            self.registrationStatusLabel.text = f"registration: {exc}"
            self.realTargetLabel.text = (
                "mapped target: none (registration failed)")
            return

        self.registrationFit = fit
        self.realTargetLabel.text = (
            "mapped target: none — place a target and press Map")
        text = "registration: " + registration.fit_note(fit)
        try:
            import json

            payload = registration.fit_payload(fit, config)
            payload["when"] = datetime.now().isoformat(timespec="seconds")
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            path = LOGS_DIR / "registration-solved.json"
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n",
                                 encoding="utf-8")
            temporary.replace(path)
            text += f"\nsolved map written to {path}"
        except OSError as exc:
            text += f"\nWARNING: could not write the solved map: {exc}"
        self.registrationStatusLabel.text = text

    def onRegistrationMap(self, checked=False):
        """Map the last placed virtual target to the real frame."""
        from datetime import datetime

        from targeting.registration import apply_transform

        if self.registrationFit is None:
            self.realTargetLabel.text = (
                "mapped target: fit the markers first")
            return
        if self.lastCube is None:
            self.realTargetLabel.text = (
                "mapped target: place a target in the virtual scene first")
            return
        try:
            real = apply_transform(self.registrationFit, self.lastCube)
        except (KeyError, TypeError, ValueError) as exc:
            self.realTargetLabel.text = f"mapped target: {exc}"
            return
        self.lastReal = tuple(float(value) for value in real)
        frame = self.registrationFit["real_frame"]
        cube = self.lastCube
        self.realTargetLabel.text = (
            f"real {frame}: ({real[0]:.2f}, {real[1]:.2f}, {real[2]:.2f}) mm\n"
            f"virtual cube: ({cube[0]:.2f}, {cube[1]:.2f}, {cube[2]:.2f}) mm"
        )
        try:
            import json

            payload = {
                "when": datetime.now().isoformat(timespec="seconds"),
                "real_frame": frame,
                "virtual_mm": [float(value) for value in cube],
                "real_mm": [float(value) for value in real],
            }
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            path = LOGS_DIR / "registration-last.json"
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n",
                                 encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            self.registrationStatusLabel.text += (
                f"\nWARNING: could not write the last map: {exc}")

    def onRegistrationSave(self, checked=False):
        from targeting import registration

        try:
            config = self._registration_from_panel()
            path = registration.save_registration(
                config, registration.DEFAULT_PATH)
        except (RuntimeError, OSError) as exc:
            self.registrationStatusLabel.text = f"registration: {exc}"
            return
        self.registrationStatusLabel.text = (
            f"registration saved to {path}"
            + ("" if config["fittable"] else f" ({config['note']})"))

    # -- the live camera stream ------------------------------------------

    def onCameraLayoutToggled(self, checked):
        if self._layout_busy:
            return
        self._show_camera_layout(bool(checked))

    def _sync_layout_button(self):
        """Match the button to the arrangement on screen, without switching."""
        try:
            active = self.logic.camera_layout_active()
        except Exception:  # noqa: BLE001 - a label, not a state change
            active = False
        self._layout_busy = True
        try:
            self.cameraLayoutButton.checked = active
        finally:
            self._layout_busy = False

    def _show_camera_layout(self, enabled):
        """Switch the arrangement, then re-seat the click filters.

        The camera panes only exist while this layout is showing, so the
        filters are installed after the switch and the ticked click mode is
        re-applied to whatever is now on screen.
        """
        self._layout_busy = True
        try:
            # Filters are seated on widgets that the switch may replace, so
            # they come off first and go back on whatever is now showing. The
            # ticked click modes are kept: a layout change is not a mode change.
            self.remove_filters(reset_modes=False)
            self.logic.set_camera_layout(bool(enabled))
            self.cameraLayoutButton.checked = bool(enabled)
            self.install_filters()
        except Exception as exc:            # noqa: BLE001 - shown in the panel
            self.streamLabel.text = f"layout: {exc}"
            return
        finally:
            self._layout_busy = False
        self.refresh_filters()

    def onConnectRealCameras(self):
        """One button: camera layout, then the sender, then the connection.

        The layout comes first: binding a volume needs the camera panes to
        exist, and a camera picture is only ever shown in its own view.
        """
        self._show_camera_layout(True)
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
        # The camera workspace closes with the cameras; the layout button
        # brings it back without restarting the sender.
        self._show_camera_layout(False)

    def onConnectCameraStream(self):
        # The panes are where the volumes land: show the layout first.
        self._show_camera_layout(True)
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
        self._show_camera_layout(False)

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
        self.lastCube = None
        self.lastReal = None
        self.realTargetLabel.text = (
            "mapped target: none — place a target in the virtual scene")
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
