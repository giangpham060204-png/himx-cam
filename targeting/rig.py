"""Where the camera rig sits relative to what Slicer shows.

The cameras measure in their own frame: millimetres out of the triangulation,
axes wherever the rig happens to be bolted down. Slicer talks in RAS. Between
the two sits one rigid transform, and it is written down in a file rather than
assumed -- because the real rig will have to be registered into the patient
frame, and hiding that until later would make the simulated flow easier than
reality and hide its hardest problem.

Pure standard library: this package runs on a bare interpreter and inside Slicer.

Sign conventions, checked against tests/test_rig.py:
  R = Rz @ Ry @ Rx, applied to the point first, then the translation is added.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

Point = tuple


@dataclass(frozen=True)
class Rig:
    """A rigid placement: rotate the point, then translate it."""

    translation_mm: Point = (0.0, 0.0, 0.0)
    rotation_deg: Point = (0.0, 0.0, 0.0)

    def to_ras(self, point):
        """RAS coordinates of a point measured in the rig's own frame."""
        return _add(_apply(_rotation_matrix(self.rotation_deg), point),
                    self.translation_mm)

    def to_rig(self, point):
        """Rig coordinates of an RAS point: the same step backwards.

        The inverse rotation is the transpose, not the same angles negated:
        matrix products do not commute, and the negated-angle shortcut is wrong
        for any rotation mixing two axes.
        """
        matrix = _transpose(_rotation_matrix(self.rotation_deg))
        return _apply(matrix, _subtract(point, self.translation_mm))


def _rotation_matrix(angles_deg):
    """R = Rz @ Ry @ Rx for an XYZ Euler triple in degrees."""
    rx, ry, rz = (math.radians(angle) for angle in angles_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )


def _apply(matrix, point):
    return tuple(sum(matrix[i][j] * point[j] for j in range(3)) for i in range(3))


def _transpose(matrix):
    return tuple(tuple(matrix[j][i] for j in range(3)) for i in range(3))


def _add(a, b):
    return tuple(a[i] + b[i] for i in range(3))


def _subtract(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def load_rig(path):
    """Read a rig placement. Raises on a missing or malformed file.

    No defaults and no fallback: a rig quietly left at the origin would put
    every measured point in the wrong place, and look perfectly healthy doing it.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise RuntimeError(f"cannot read the rig placement at {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from None

    try:
        translation = tuple(float(value) for value in data["translation_mm"])
        rotation = tuple(float(value) for value in data["rotation_deg"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{path} must hold translation_mm and rotation_deg, "
            f"three numbers each: {exc}"
        ) from None
    if len(translation) != 3 or len(rotation) != 3:
        raise RuntimeError(
            f"{path} must hold three numbers for each of translation_mm and rotation_deg"
        )
    return Rig(translation_mm=translation, rotation_deg=rotation)
