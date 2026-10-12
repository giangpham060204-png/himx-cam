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

        self.pickBox.toggled.connect(self.onPickToggled)
        self.clearButton.clicked.connect(self.onClear)
        self.openLogButton.clicked.connect(self.onOpenLog)

        self.layout.addStretch(1)

    # -- lifecycle ----------------------------------------------------------

    def enter(self):
        self.install_filters()
        self.refresh_status()

    def exit(self):
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

    def set_pick_enabled(self, enabled):
        for _widget, view_filter in self.filters.values():
            view_filter.enabled = bool(enabled)

    # -- handlers -------------------------------------------------------------

    def onPickToggled(self, checked):
        self.set_pick_enabled(checked)

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

    def refresh_status(self):
        self.statusLabel.text = "Ready - tick Pick targets and click in a view."
