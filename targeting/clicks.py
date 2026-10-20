"""The three camera pixels Slicer publishes, and the view arithmetic behind them.

Slicer has no OpenCV, so no detector runs there; what it can do is show a picture
and say where a person clicked on it. This module is the pure half of that: read
the file it publishes, and turn a position in a slice view into the picture pixel
under it. Both matrices come from Slicer, so the arithmetic can be tested here
with synthetic ones -- including the flip that would otherwise be found by a
point being wrong.

Standard library only, because Slicer's Python has no third-party packages to
spare and this file is imported there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CLICKS_FILE = "camera-clicks.json"


@dataclass(frozen=True)
class Clicks:
    """One entry per camera view, in camera order; None is a view not clicked yet."""

    pixels: tuple
    when: str = ""

    def is_complete(self) -> bool:
        """True once every view has been clicked; a point needs all three."""
        return all(pixel is not None for pixel in self.pixels)


def read_clicks(path) -> tuple:
    """``(Clicks | None, note)`` from one published file.

    A pixel is null until its view is clicked, so a file may hold a partial set;
    only a complete set can solve a point, which is what ``is_complete`` is for.
    The all-null file is the explicit empty set: the clear signal, not an error.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"no clicks at {path.name} ({exc})"
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return None, f"{path.name}: not a clicks file ({exc})"
    if not isinstance(payload, dict) or "pixels" not in payload:
        return None, f"{path.name}: not a clicks file (no pixels)"
    raw = payload["pixels"]
    if not isinstance(raw, list) or len(raw) != 3:
        return None, f"{path.name}: expected three pixels, one per camera"
    parsed = []
    for entry in raw:
        if entry is None:
            parsed.append(None)
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            try:
                parsed.append((float(entry[0]), float(entry[1])))
            except (TypeError, ValueError):
                return None, f"{path.name}: pixels must be numbers"
        else:
            return None, f"{path.name}: a pixel is null until its view is clicked"
    pixels = tuple(parsed)
    when = str(payload.get("when", ""))
    if all(pixel is None for pixel in pixels):
        return Clicks(pixels=pixels, when=when), \
            f"{path.name}: empty clicks (no view clicked yet)"
    return Clicks(pixels=pixels, when=when), \
        f"{path.name}: clicks read"


def pixel_from_view(slice_to_ras, ras_to_ijk, x, y) -> tuple:
    """The picture pixel under a click, through Slicer's two matrices.

    ``SliceToRAS`` takes the view's own xy (millimetres, y down) into RAS, and
    the volume's ``RASToIJK`` takes RAS into the array, whose first two indices
    are (column, row) -- the camera's (u, v). A singular view is refused
    instead of producing a plausible pixel: it collapses positions that must
    stay distinct, so there is no mapping to apply.

    (Deviates from the brief's code body on one line: the brief inverts
    ``slice_to_ras`` before applying it, which contradicts the brief's own
    scale test -- inverse of [[2,0,0,5],[0,2,0,7],...] at (10, 10) is
    (2.5, 1.5), not the expected (25.0, 27.0) -- and contradicts Slicer's
    documented XYToRAS direction, "mapping from XY ... into RAS". The
    singularity refusal is kept via the same inverse-existence check.)
    """
    if _inverse4(slice_to_ras) is None:
        raise ValueError("the view has no mapping")
    ras = _apply(slice_to_ras, (float(x), float(y), 0.0, 1.0))
    index = _apply(ras_to_ijk, ras)
    return float(index[0]), float(index[1])


def _apply(matrix, vector):
    return tuple(sum(float(matrix[row][column]) * vector[column] for column in range(4))
                 for row in range(4))


def _inverse4(matrix):
    """The inverse of a 4x4 nested sequence, or None when it has none."""
    size = 4
    work = [[float(matrix[row][column]) for column in range(size)]
            + [1.0 if row == column else 0.0 for column in range(size)]
            for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) < 1e-12:
            return None
        work[column], work[pivot] = work[pivot], work[column]
        scale = work[column][column]
        work[column] = [value / scale for value in work[column]]
        for row in range(size):
            if row != column and work[row][column]:
                factor = work[row][column]
                work[row] = [value - factor * other
                             for value, other in zip(work[row], work[column])]
    return [row[size:] for row in work]
