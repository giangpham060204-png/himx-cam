"""The marker plate: its declared geometry and the pixel-to-plate map.

Pure standard library plus numpy at call time: no Slicer, no camera, no clock.
The panel, the console tool and the tests all read the same numbers through
this file, so a marker cannot mean one thing in the JSON and another in the
scene.

Coordinates are millimetres on the plate: origin at the plate's lower-left
corner, x right, y up. z is optional and defaults to 0 -- a constant z is still
one plane, so the map stays valid; a varying z means the markers are not
coplanar and the map is refused rather than quietly approximated.
"""

from __future__ import annotations

import json
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "marker-plate.json"

# One picture pixel of marker-centre error is already a visible registration
# error; above this the config and the picture are not describing one plate.
MAX_RESIDUAL_MM = 1.0
# Markers within this z range are treated as one plane.
COPLANAR_TOLERANCE_MM = 1.0


def validate_plate(data, source="<memory>"):
    """The plate dict for already-parsed data, validated. Raises RuntimeError.

    Every failure names the source and the key, so the panel can show the same
    sentence the console prints.
    """
    try:
        size = data["plate_mm"]
        width = float(size["width"])
        height = float(size["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{source}: plate_mm must hold numeric width and height: {exc}"
        ) from None
    if width <= 0.0 or height <= 0.0:
        raise RuntimeError(f"{source}: the plate must have a positive size, "
                           f"got {width} x {height} mm")

    raw_markers = data.get("markers")
    if not isinstance(raw_markers, list) or len(raw_markers) < 4:
        raise RuntimeError(
            f"{source}: markers must be a list of at least four; a homography "
            f"needs four points and the fifth is what checks the fit")

    markers = []
    for index, entry in enumerate(raw_markers, start=1):
        try:
            label = str(entry["label"])
            values = [float(value) for value in entry["mm"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{source}: marker {index} needs a label and two or three "
                f"numbers in mm: {exc}") from None
        if len(values) not in (2, 3):
            raise RuntimeError(
                f"{source}: marker {index} has {len(values)} numbers; use "
                f"[x, y] or [x, y, z]")
        x, y = values[0], values[1]
        z = values[2] if len(values) == 3 else 0.0
        if not (0.0 <= x <= width and 0.0 <= y <= height):
            raise RuntimeError(
                f"{source}: marker {label} at ({x}, {y}) mm is outside the "
                f"{width} x {height} mm plate")
        markers.append({"label": label, "mm": (x, y), "z": z})

    labels = [marker["label"] for marker in markers]
    repeated = sorted({label for label in labels if labels.count(label) > 1})
    if repeated:
        raise RuntimeError(f"{source}: repeated marker labels {repeated}; "
                           f"labels key the solved map and must be unique")

    for first in range(len(markers)):
        for second in range(first + 1, len(markers)):
            a, b = markers[first]["mm"], markers[second]["mm"]
            if (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 < 1e-4:
                raise RuntimeError(
                    f"{source}: markers {labels[first]} and {labels[second]} "
                    f"sit at the same point; two markers cannot give two "
                    f"equations")

    # A homography fitted to collinear points is exact and meaningless: four
    # points on a line have a zero residual and no idea of the second axis.
    largest = 0.0
    for i in range(len(markers)):
        ax, ay = markers[i]["mm"]
        for j in range(i + 1, len(markers)):
            bx, by = markers[j]["mm"]
            for k in range(j + 1, len(markers)):
                cx, cy = markers[k]["mm"]
                largest = max(largest,
                              abs((bx - ax) * (cy - ay)
                                  - (cx - ax) * (by - ay)) / 2.0)
    if largest < 1.0:
        raise RuntimeError(
            f"{source}: the markers are collinear (largest triangle "
            f"{largest:.3f} mm2); a homography needs a real spread")

    z_spread = (max(marker["z"] for marker in markers)
                - min(marker["z"] for marker in markers))

    try:
        origin = tuple(float(value) for value in data["cube_origin_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{source}: cube_origin_mm must hold three numbers: {exc}") from None
    if len(origin) != 3:
        raise RuntimeError(f"{source}: cube_origin_mm needs three numbers, "
                           f"got {len(origin)}")

    return {"width": width, "height": height, "markers": markers,
            "origin": origin, "path": str(source),
            "z_spread": z_spread,
            "coplanar": z_spread <= COPLANAR_TOLERANCE_MM}


def load_plate(path=DEFAULT_PATH):
    """Read and validate one plate file."""
    path = pathlib.Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"cannot read the plate at {path}: {exc}") from None
    except ValueError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from None
    plate = validate_plate(data, path)
    plate["path"] = str(path)
    return plate


def plate_to_data(plate):
    """The plate dict back in the file's shape, for saving."""
    return {
        "plate_mm": {"width": round(float(plate["width"]), 3),
                     "height": round(float(plate["height"]), 3)},
        "markers": [
            {"label": marker["label"],
             "mm": [round(float(marker["mm"][0]), 3),
                    round(float(marker["mm"][1]), 3)]
                   + ([round(float(marker["z"]), 3)] if marker["z"] else [])}
            for marker in plate["markers"]
        ],
        "cube_origin_mm": [round(float(value), 3) for value in plate["origin"]],
    }


def save_plate(plate, path=DEFAULT_PATH):
    """Write the plate file atomically. Returns the path written.

    A ``note`` already in the file is kept: the placeholder warning is not the
    panel's to delete just because it saved the same numbers back.
    """
    path = pathlib.Path(path)
    data = plate_to_data(plate)
    try:
        note = json.loads(path.read_text(encoding="utf-8"))["note"]
    except (OSError, ValueError, KeyError):
        note = None
    if isinstance(note, str) and note:
        data = {"note": note, **data}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def coplanarity_note(plate):
    """Why the pixel map cannot be fitted, or None when it can."""
    if plate["coplanar"]:
        return None
    return (f"the markers are not coplanar (z spans {plate['z_spread']:.2f} "
            f"mm); the pixel map needs one plane, so no map was fitted")


def fit_homography(pixels, plate_mm):
    """The 3x3 map from picture pixels to plate millimetres.

    Direct linear transform with h22 fixed at 1, solved by least squares, so
    four points give the exact answer and five or more give the same answer
    plus a residual worth reading.
    """
    import numpy as np  # noqa: PLC0415 - only this needs it

    if len(pixels) != len(plate_mm):
        raise ValueError(f"{len(pixels)} pixels for {len(plate_mm)} markers")
    if len(pixels) < 4:
        raise ValueError(f"a homography needs four markers, got {len(pixels)}")

    rows, rhs = [], []
    for (u, v), (x, y) in zip(pixels, plate_mm):
        rows.append([u, v, 1.0, 0.0, 0.0, 0.0, -x * u, -x * v])
        rhs.append(x)
        rows.append([0.0, 0.0, 0.0, u, v, 1.0, -y * u, -y * v])
        rhs.append(y)

    solution, *_ = np.linalg.lstsq(np.asarray(rows, float),
                                   np.asarray(rhs, float), rcond=None)
    return ((float(solution[0]), float(solution[1]), float(solution[2])),
            (float(solution[3]), float(solution[4]), float(solution[5])),
            (float(solution[6]), float(solution[7]), 1.0))


def to_plate(homography, u, v):
    """The plate millimetres under one picture pixel."""
    w = homography[2][0] * u + homography[2][1] * v + homography[2][2]
    if abs(w) < 1e-12:
        raise ValueError("that pixel has no plate position: the map is singular")
    return ((homography[0][0] * u + homography[0][1] * v + homography[0][2]) / w,
            (homography[1][0] * u + homography[1][1] * v + homography[1][2]) / w)
