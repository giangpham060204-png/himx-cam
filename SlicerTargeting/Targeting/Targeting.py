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
            form.addRow(f"{tag} pixel (u, v)", row)
            self.cameraPixelEdits[widget_name] = (u_edit, v_edit)
            u_edit.editingFinished.connect(
                lambda _w=widget_name: self.on_camera_pixel_edited(_w))
            v_edit.editingFinished.connect(
                lambda _w=widget_name: self.on_camera_pixel_edited(_w))

        self.cameraPixelNote = qt.QLabel("")
        self.cameraPixelNote.wordWrap = True
        form.addRow(self.cameraPixelNote)

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
        form.addRow("Spawn 3D point (rig mm)", spawn_row)

        self.spawnNote = qt.QLabel("")
        self.spawnNote.wordWrap = True
        form.addRow(self.spawnNote)

        self.pickBox.toggled.connect(self.onPickToggled)
        self.probeBox.toggled.connect(self.onProbeToggled)
        self.cameraBox.toggled.connect(self.onCameraToggled)
        self.clearButton.clicked.connect(self.onClear)
        self.openLogButton.clicked.connect(self.onOpenLog)
        self.loadExternalButton.clicked.connect(self.onLoadExternal)
        self.loadCameraViewsButton.clicked.connect(
            lambda _checked=False: self.onLoadCameraViews())
        self.clearCameraButton.clicked.connect(self.onClearCameraClicks)
        self.spawnButton.clicked.connect(self.on_spawn)

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
            try:
                timer = self.logic.watch_camera_clicks()
                try:
                    timer.timeout.disconnect(self._refresh_camera_label)
                except Exception:
                    pass
                timer.timeout.connect(self._refresh_camera_label)
            except Exception:
                pass

    def _refresh_camera_label(self):
        try:
            self.cameraLabel.text = self.logic.camera_status()
        except Exception:
            pass
        self._refresh_camera_pixel_fields()
        self._update_identical_warning()

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
        self.cameraPixelNote.text = ""
        self._refresh_camera_pixel_fields()

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
        self.statusLabel.text = "Ready - tick Pick targets and click in a view."
