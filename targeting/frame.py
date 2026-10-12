"""Frame constants: everything the converter needs to know about the setup.

A frame answers three questions: where the origin is, which way each axis points,
and what unit each number is in. All values here are millimetres.

Pixel coordinates are measured from the VIEW CENTRE, not from a corner. Slicer
centres a slice view on the slice origin, so the centre pixel is exactly the
field centre and needs no assumed offset. A corner would need the field's
position inside the view, which nothing here knows.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Tuple

VIEWS = ("XY", "YZ", "XZ")
RAS_AXES = ("R", "A", "S")

# On-screen direction -> (RAS axis, sign). Measured from Slicer, never guessed.
AxisBinding = Dict[str, Tuple[str, int]]

# (u_centre_px, v_centre_px) per view.
ViewCentre = Dict[str, Tuple[float, float]]


@dataclass
class Frame:
    """Field geometry plus the on-screen direction map for each view."""

    field_size_mm: Dict[str, float]
    field_center_ras: Dict[str, float]
    pixel_size_mm: Dict[str, float]
    view_axis_map: Dict[str, AxisBinding]
    view_center_px: Dict[str, ViewCentre] = field(default_factory=dict)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "Frame":
        data = json.loads(Path(path).read_text())
        centres = {
            view: tuple(float(c) for c in coords)
            for view, coords in data.get("view_center_px", {}).items()
        }
        return cls(
            field_size_mm={k: float(v) for k, v in data["field_size_mm"].items()},
            field_center_ras={k: float(v) for k, v in data["field_center_ras"].items()},
            # Screen-dependent fields are no longer stored; they are derived
            # from the live Slicer views at runtime. Empty means "not resolved
            # yet", and the converter says so clearly if asked to work without
            # them.
            pixel_size_mm={k: float(v) for k, v in data.get("pixel_size_mm", {}).items()},
            view_axis_map={
                view: {name: (axis, int(sign)) for name, (axis, sign) in binding.items()}
                for view, binding in data["view_axis_map"].items()
            },
            view_center_px=centres,
        )


def make_test_frame(
    field_size_mm: float,
    pixel_size_mm: float,
    field_center_ras: Mapping[str, float] | None = None,
) -> Frame:
    """Build a Frame with the naive axis map, for tests and for a first guess.

    The naive map says screen-right is +X and screen-down is +Y for every view,
    and that the view centre is pixel (0,0). Real Slicer views differ; the
    runtime mapping is read from the live views by slicer_bridge.live_frame().
    """
    center = dict(field_center_ras or {"R": 0.0, "A": 0.0, "S": 0.0})
    size = {axis: float(field_size_mm) for axis in RAS_AXES}
    view_axes = {
        "XY": {"u": ("R", +1), "v": ("A", +1), "depth": ("S", +1)},
        "YZ": {"u": ("A", +1), "v": ("S", +1), "depth": ("R", +1)},
        "XZ": {"u": ("R", +1), "v": ("S", +1), "depth": ("A", +1)},
    }
    return Frame(
        field_size_mm=size,
        field_center_ras=center,
        pixel_size_mm={view: float(pixel_size_mm) for view in VIEWS},
        view_axis_map=view_axes,
        view_center_px={view: (0.0, 0.0) for view in VIEWS},
    )