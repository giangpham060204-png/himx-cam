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
            "Tick Pick targets, click a point in Red/Green/Yellow; the slice "
            "slider gives the third coordinate. The Virtual -> real section "
            "maps it through measured markers to the real frame."
        )
        parent.acknowledgementText = ""


class TargetingWidget(ScriptedLoadableModuleWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.logic = TargetingLogic()
        self.filters = {}
        self.lastClick = None
        self.lastCube = None
        self.lastReal = None
        self.registrationFit = None
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
            "the clicked pixel and the slice slider's depth. Targets are the "
            "blue spheres (not fiducial markers) and can be dragged in the 3D "
            "view; the orange plate markers are locked.")
        pick_row = qt.QHBoxLayout()
        pick_row.addWidget(self.pickBox)
        self.clearButton = qt.QPushButton("Clear targets")
        self.clearButton.toolTip = (
            "Remove all blue target points; the plate and registration "
            "markers stay")
        pick_row.addWidget(self.clearButton)
        form.addRow(pick_row)

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
        self.videoSettings.collapsed = True
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

        # -- the fiducial plate -----------------------------------------
        # The plate is the reference marker set for the camera check and the
        # declared frame. Everything is editable here; the JSON is the file.
        self.markerPanel = ctk.ctkCollapsibleButton()
        self.markerPanel.text = "Fiducial plate"
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
        self.markerSaveButton.toolTip = (
            "Write marker-plate.json; the file auto-loads next time")
        marker_file_row.addWidget(self.markerSaveButton)
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
            "appears at the top of the panel after Map. B1/B2/T1 are just "
            "labels: here they mean the two balls and the triangle; rename "
            "them to match the physical markers. Double-click a cell to edit "
            "it.")
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
        self.registrationFitButton = qt.QPushButton("Fit")
        self.registrationMapButton = qt.QPushButton("Map last target")
        self.registrationFitButton.toolTip = (
            "Fit the rigid virtual-to-real transform and show the residual")
        self.registrationMapButton.toolTip = (
            "Map the last placed target from the virtual cube to the real "
            "frame")
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
        self.registrationSaveButton.toolTip = (
            "Write marker-registration.json; the file auto-loads next time")
        registration_file_row.addWidget(self.registrationSaveButton)
        registration_form.addRow(registration_file_row)

        self.registrationStatusLabel = qt.QLabel("registration: not fitted")
        self.registrationStatusLabel.wordWrap = True
        registration_form.addRow(self.registrationStatusLabel)


        self.pickBox.toggled.connect(self.onPickToggled)
        self.clearButton.clicked.connect(self.onClear)
        self.realCameraButton.clicked.connect(self.onConnectRealCameras)
        self.realCameraStopButton.clicked.connect(self.onStopRealCameras)
        self.cameraLayoutButton.toggled.connect(self.onCameraLayoutToggled)
        self.markerAddButton.clicked.connect(self.onMarkerAdd)
        self.markerRemoveButton.clicked.connect(self.onMarkerRemove)
        self.markerApplyButton.clicked.connect(self.onMarkerApply)
        self.markerSaveButton.clicked.connect(self.onMarkerSave)
        self.registrationFitButton.clicked.connect(self.onRegistrationFit)
        self.registrationMapButton.clicked.connect(self.onRegistrationMap)
        self.registrationSaveButton.clicked.connect(self.onRegistrationSave)
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
            video = self.logic.video_status()
        except Exception:
            video = {"status": "off", "message": "cameras: off"}
        self.videoLabel.text = video["message"]
        self.realCameraStopButton.setEnabled(video["status"] == "running")

    def exit(self):
        self._stop_video_quietly()
        self.set_pick_enabled(False)
        self.remove_filters()

    def cleanup(self):
        self._stop_video_quietly()
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
        """Attach the pick filter to the standard slice views.

        The camera panes are pictures, not coordinate spaces: they have no
        click filter. The 3D view keeps only the Ctrl+drag orbit filter.
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

        if self.threeDFilter is None:
            three_d = layout.threeDWidget(0)
            if three_d is not None:
                three_view = three_d.threeDView()
                target = self._mouse_target(three_view)
                three_filter = ThreeDViewClickFilter(three_view)
                target.installEventFilter(three_filter)
                self.threeDFilter = (target, three_filter)

    def remove_filters(self, reset_modes=True):
        for target, view_filter in self.filters.values():
            try:
                target.removeEventFilter(view_filter)
            except RuntimeError:
                # The layout switch already deleted the widget underneath it.
                pass
        self.filters = {}
        if self.threeDFilter is not None:
            target, three_filter = self.threeDFilter
            try:
                target.removeEventFilter(three_filter)
            except RuntimeError:
                pass
            self.threeDFilter = None
        if reset_modes:
            self.pickBox.checked = False

    def set_pick_enabled(self, enabled):
        for _widget, view_filter in self.filters.values():
            view_filter.click_enabled = bool(enabled)

    def refresh_filters(self):
        self.set_pick_enabled(self.pickBox.checked)

    def onPickToggled(self, checked):
        self.refresh_filters()

    def on_click(self, view, u_px, v_px):
        self.lastClick = (view, u_px, v_px)
        try:
            result = self.logic.place_from_click(view, u_px, v_px)
        except Exception as exc:  # noqa: BLE001 - a click must never crash Slicer
            import traceback  # noqa: PLC0415

            traceback.print_exc()
            self.statusLabel.text = f"Internal error: {exc} (details in console)"
            return

        if result["status"] == "placed":
            cube = result["cube"]
            self.lastCube = tuple(float(value) for value in cube)
            self.lastReal = None
            self.realTargetLabel.text = (
                "real target: none — press Map for this target")
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

    def _fill_marker_table(self, plate):
        """Show a validated plate in the panel fields."""
        self.markerTable.setRowCount(0)
        for marker in plate["markers"]:
            row = self.markerTable.rowCount
            self.markerTable.insertRow(row)
            self.markerTable.setItem(
                row, 0, qt.QTableWidgetItem(marker["label"]))
            for column, value in ((1, marker["mm"][0]),
                                  (2, marker["mm"][1]),
                                  (3, marker["z"])):
                self.markerTable.setItem(
                    row, column, qt.QTableWidgetItem(f"{value:g}"))
        self.markerWidthSpin.value = plate["width"]
        self.markerHeightSpin.value = plate["height"]
        (self.markerOriginXSpin.value, self.markerOriginYSpin.value,
         self.markerOriginZSpin.value) = plate["origin"]

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

    @staticmethod
    def _current_row(table):
        """The selected row, whether Qt exposes currentRow as int or slot."""
        value = table.currentRow
        if callable(value):
            value = value()
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def onMarkerRemove(self, checked=False):
        row = self._current_row(self.markerTable)
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

    # -- the virtual -> real registration --------------------------------

    def onRegistrationEdited(self, *_args):
        """Any edit invalidates the fitted transform until Fit is pressed."""
        if self.registrationFit is not None:
            self.registrationFit = None
            self.registrationStatusLabel.text = (
                "registration: edited; fit the markers again")
            self.realTargetLabel.text = (
                "real target: none (registration edited)")

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
        self.realTargetLabel.text = "real target: none (registration loaded)"

    def onRegistrationAdd(self, checked=False):
        row = self.registrationTable.rowCount
        self.registrationTable.insertRow(row)
        self.registrationTable.setItem(
            row, 0, qt.QTableWidgetItem(f"M{row + 1}"))
        for column in range(1, 7):
            self.registrationTable.setItem(row, column,
                                           qt.QTableWidgetItem("0"))

    def onRegistrationRemove(self, checked=False):
        row = self._current_row(self.registrationTable)
        if row < 0:
            row = self.registrationTable.rowCount - 1
        if row >= 0:
            self.registrationTable.removeRow(row)

    def onRegistrationFit(self, checked=False):
        """Fit the rigid transform and write the solved map beside the logs."""
        from datetime import datetime

        from targeting import registration

        try:
            config = self._registration_from_panel()
        except RuntimeError as exc:
            self.registrationStatusLabel.text = f"registration: {exc}"
            self.realTargetLabel.text = (
                "real target: none (registration invalid)")
            return
        if not config["fittable"]:
            self.registrationFit = None
            self.registrationStatusLabel.text = f"registration: {config['note']}"
            self.realTargetLabel.text = (
                "real target: none (registration not fittable)")
            return
        try:
            fit = registration.fit_registration(config)
        except (RuntimeError, ValueError) as exc:
            self.registrationFit = None
            self.registrationStatusLabel.text = f"registration: {exc}"
            self.realTargetLabel.text = (
                "real target: none (registration failed)")
            return

        self.registrationFit = fit
        self.realTargetLabel.text = (
            "real target: none — place a target and press Map")
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
                "real target: fit the markers first")
            return
        try:
            cube = self.logic.last_target_cube()
        except Exception:  # noqa: BLE001 - fall back to the click cache
            cube = None
        if cube is None:
            cube = self.lastCube
        if cube is None:
            self.realTargetLabel.text = (
                "real target: place a target in the virtual scene first")
            return
        self.lastCube = tuple(float(value) for value in cube)
        try:
            real = apply_transform(self.registrationFit, self.lastCube)
        except (KeyError, TypeError, ValueError) as exc:
            self.realTargetLabel.text = f"real target: {exc}"
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
            self.videoLabel.text = f"layout: {exc}"
            return
        finally:
            self._layout_busy = False
        self.refresh_filters()

    def onConnectRealCameras(self):
        """One button: camera layout, then the sender, then the connection."""
        self._show_camera_layout(True)
        device = self.realCameraDeviceEdit.text.strip() or "/dev/video0"
        try:
            connected = self.logic.connect_camera_stream(
                port=int(self.portSpin.value))
        except Exception as exc:            # noqa: BLE001 - shown in the panel
            self.videoLabel.text = f"cameras: could not connect: {exc}"
            return
        self.videoLabel.text = connected["message"]

        try:
            result = self.logic.start_real_cameras(
                device=device, fps=float(self.fpsSpin.value))
        except Exception as exc:            # noqa: BLE001 - shown in the panel
            self.videoLabel.text = f"cameras: could not start: {exc}"
            return
        self.videoLabel.text = result["message"]
        self.realCameraStopButton.setEnabled(result["status"] == "running")
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
            self.logic.disconnect_camera_stream()
        except Exception:
            pass
        self._show_camera_layout(False)

    def onClear(self, checked=False):
        self.logic.clear_targets()
        self.lastCube = None
        self.lastReal = None
        self.realTargetLabel.text = (
            "real target: none — fit the markers, place a target, then Map")

    def refresh_status(self):
        self.statusLabel.text = (
            "Tick Pick targets, click a point in Red/Green/Yellow; the slice "
            "slider gives the depth. Fit the virtual -> real markers, then "
            "Map last target.")
