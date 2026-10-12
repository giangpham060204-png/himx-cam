"""Read the live Slicer slice views and build a Frame.

This is the only module in the project that imports `slicer`. Everything in
`targeting/` is pure arithmetic, which is what keeps it testable on this
laptop; this module translates Slicer's live state into the plain numbers the
pure layer wants.

The mapping is read fresh on every call, so resizing the window, zooming,
loading a volume or moving to another monitor needs no recalibration by
anyone. A Frame built here is only valid while the scene is as it is now,
which is exactly what a touch-interaction handler needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from targeting.frame import Frame
from targeting.view_mapping import frame_from_views

FRAME_PATH = Path(__file__).resolve().parents[1] / "frame.json"

# Slicer's default slice views and their anatomical planes: Red axial
# (R-A), Green coronal (R-S), Yellow sagittal (A-S).
VIEW_WIDGETS = {"XY": "Red", "XZ": "Green", "YZ": "Yellow"}

WIDGET_VIEWS = {widget: view for view, widget in VIEW_WIDGETS.items()}


class MissingSliceView(RuntimeError):
    """Raised when the Red, Green or Yellow slice view does not exist."""


def read_view_data(layout=None) -> Dict[str, Dict]:
    """Read each slice view's live pixel->RAS matrix and size from Slicer.

    Returns plain dicts of lists and numbers, so nothing downstream depends on
    Slicer objects.
    """
    if layout is None:
        import slicer  # noqa: PLC0415 - only imported when actually running in Slicer

        layout = slicer.app.layoutManager()

    data: Dict[str, Dict] = {}
    missing = []
    for view, widget_name in VIEW_WIDGETS.items():
        widget = layout.sliceWidget(widget_name)
        if widget is None:
            missing.append(widget_name)
            continue
        slice_node = widget.sliceLogic().GetSliceNode()
        slice_view = widget.sliceView()
        matrix = slice_node.GetXYToRAS()
        slice_to_ras = slice_node.GetSliceToRAS()
        dimensions = slice_node.GetDimensions()
        data[view] = {
            "xy_to_ras": [
                [matrix.GetElement(row, col) for col in range(4)]
                for row in range(4)
            ],
            "slice_origin_ras": [slice_to_ras.GetElement(row, 3) for row in range(3)],
            "width_px": float(dimensions[0]),
            "height_px": float(dimensions[1]),
            "qt_width_px": float(slice_view.width),
            "qt_height_px": float(slice_view.height),
        }
    if missing:
        raise MissingSliceView(
            f"slice view(s) not found: {', '.join(missing)}. "
            "Use a layout that shows all three slice views (for example Four-Up)."
        )
    return data


def live_frame(frame_path: Path = FRAME_PATH, layout=None) -> Frame:
    """A Frame whose pixel geometry was read from the live Slicer views."""
    base = Frame.load(frame_path)
    return frame_from_views(base, read_view_data(layout=layout))
