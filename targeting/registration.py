"""Rigid virtual-to-real registration from measured fiducial markers.

Pure standard library plus numpy at call time: no Slicer, no camera, no clock.
The panel, the console tool and the tests all read the same numbers through
this file, so a marker cannot mean one thing in the JSON and another in the
scene.

Both frames are written down here: ``virtual_mm`` is where the marker is in
the Slicer cube, and ``real_mm`` is where the same marker was measured in the
real frame. The transform is rigid: rotation + translation, no scale and no
reflection. Three non-collinear markers determine it. Three points give nine
equations for six unknowns, so the residual is meaningful even with the
minimum; four or more make it a stronger check. A uniform scale error is
reported as a diagnostic but is **not** applied: a rigid device must not be
stretched to fit a bad measurement.

The registration is deliberately independent of the marker plate. The plate
is the camera homography's five-point target; a three-marker device (two balls
plus a triangle) is a different object and must not be forced through the
plate's four-point rule.
"""

from __future__ import annotations

import json
import math
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "marker-registration.json"

MIN_MARKERS = 3
# A marker-triangle thinner than this has no reliable normal: the rotation
# about the line through the points would be unconstrained.
MIN_TRIANGLE_MM2 = 1.0
# Above this the two point sets are not one rigid object.
MAX_RESIDUAL_MM = 1.0
# A rigid fit should not need a scale; beyond this the distances disagree.
MAX_SCALE_ERROR = 0.02
# Markers within this distance of their best-fit plane are treated as one
# plane. A coplanar set cannot distinguish a mirror flip from a rotation: the
# physical mounting has to keep the face the right way up.
COPLANAR_TOLERANCE_MM = 1.0


def _float(value, what, source):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"{source}: {what} must be a number, got {value!r}") from None
    if not math.isfinite(number):
        raise RuntimeError(f"{source}: {what} must be finite, got {value!r}")
    return number


def _point(entry, key, source):
    try:
        values = [_float(value, key, source) for value in entry[key]]
    except (KeyError, TypeError):
        raise RuntimeError(
            f"{source}: {key} must hold three numbers, got {entry!r}") from None
    if len(values) != 3:
        raise RuntimeError(
            f"{source}: {key} needs three numbers, got {len(values)}")
    return tuple(values)


def _triangle_area(a, b, c):
    """The area of one 3-D triangle, in square millimetres."""
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    cx = uy * vz - uz * vy
    cy = uz * vx - ux * vz
    cz = ux * vy - uy * vx
    return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)


def _largest_triangle(points):
    """The largest triangle area among the points, in square millimetres."""
    largest = 0.0
    for first in range(len(points)):
        for second in range(first + 1, len(points)):
            for third in range(second + 1, len(points)):
                largest = max(largest, _triangle_area(
                    points[first], points[second], points[third]))
    return largest


def validate_registration(data, source="<memory>"):
    """The registration dict for already-parsed data, validated.

    Each marker needs a label, its virtual cube millimetres and the same
    marker's measured real millimetres. Every failure names the source and
    the marker.
    """
    if not isinstance(data, dict):
        raise RuntimeError(f"{source}: the registration must be a JSON object")
    frame = str(data.get("real_frame", "")).strip()
    if not frame:
        raise RuntimeError(
            f"{source}: real_frame must name the measured frame (arm-base, "
            f"plate, bench, ...)")

    raw_markers = data.get("markers")
    if not isinstance(raw_markers, list) or len(raw_markers) < MIN_MARKERS:
        raise RuntimeError(
            f"{source}: markers must be a list of at least {MIN_MARKERS}; "
            f"three non-collinear markers determine a rigid transform")

    markers = []
    for index, entry in enumerate(raw_markers, start=1):
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"{source}: marker {index} must be an object with label, "
                f"virtual_mm and real_mm")
        label = str(entry.get("label", "")).strip()
        if not label:
            raise RuntimeError(f"{source}: marker {index} has no label")
        markers.append({
            "label": label,
            "virtual_mm": _point(entry, "virtual_mm", source),
            "real_mm": _point(entry, "real_mm", source),
        })

    labels = [marker["label"] for marker in markers]
    repeated = sorted({label for label in labels if labels.count(label) > 1})
    if repeated:
        raise RuntimeError(
            f"{source}: repeated marker labels {repeated}; labels key the "
            f"transform and must be unique")

    notes = []
    for key, name in (("virtual_mm", "virtual"), ("real_mm", "real")):
        largest = _largest_triangle([marker[key] for marker in markers])
        if largest < MIN_TRIANGLE_MM2:
            notes.append(
                f"the {name} markers are collinear or coincident "
                f"(largest triangle {largest:.3f} mm2)")
    note = "; ".join(notes)
    return {"real_frame": frame, "markers": markers, "path": str(source),
            "fittable": not notes, "note": note}


def load_registration(path=DEFAULT_PATH):
    """Read and validate one registration file."""
    path = pathlib.Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(
            f"cannot read the registration at {path}: {exc}") from None
    except ValueError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from None
    registration = validate_registration(data, path)
    registration["path"] = str(path)
    return registration


def registration_to_data(registration):
    """The registration back in the file's shape, for saving."""
    return {
        "real_frame": str(registration["real_frame"]),
        "markers": [
            {"label": marker["label"],
             "virtual_mm": [round(float(value), 3)
                            for value in marker["virtual_mm"]],
             "real_mm": [round(float(value), 3)
                         for value in marker["real_mm"]]}
            for marker in registration["markers"]
        ],
    }


def save_registration(registration, path=DEFAULT_PATH):
    """Write the registration atomically. Returns the path written.

    A ``note`` already in the file is kept: the placeholder warning is not the
    panel's to delete just because it saved the same numbers back.
    """
    path = pathlib.Path(path)
    data = registration_to_data(registration)
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


def fit_transform(virtual, real, labels=None):
    """The rigid transform that maps virtual points to real points.

    Kabsch/SVD: the rotation is proper (determinant +1), and the translation
    follows from the centroids. The residual is the honest check; the scale
    is reported only as a diagnostic and is never applied.
    """
    import numpy as np  # noqa: PLC0415 - only this needs it

    if len(virtual) != len(real):
        raise ValueError(f"{len(virtual)} virtual points for {len(real)} real")
    if len(virtual) < MIN_MARKERS:
        raise ValueError(
            f"a rigid transform needs at least {MIN_MARKERS} markers, "
            f"got {len(virtual)}")

    v = np.asarray(virtual, dtype=float)
    r = np.asarray(real, dtype=float)
    if v.shape != (len(virtual), 3) or r.shape != (len(real), 3):
        raise ValueError("every virtual and real point must have three numbers")

    virtual_area = _largest_triangle(virtual)
    real_area = _largest_triangle(real)
    if virtual_area < MIN_TRIANGLE_MM2:
        raise ValueError(
            f"the virtual markers are collinear or coincident (largest "
            f"triangle {virtual_area:.3f} mm2)")
    if real_area < MIN_TRIANGLE_MM2:
        raise ValueError(
            f"the real markers are collinear or coincident (largest "
            f"triangle {real_area:.3f} mm2)")

    virtual_centre = v.mean(axis=0)
    real_centre = r.mean(axis=0)
    covariance = (v - virtual_centre).T @ (r - real_centre)
    left, singular, right = np.linalg.svd(covariance)
    flip = 1.0 if np.linalg.det(right.T @ left.T) >= 0.0 else -1.0
    rotation = right.T @ np.diag([1.0, 1.0, flip]) @ left.T
    translation = real_centre - rotation @ virtual_centre

    predicted = (rotation @ v.T).T + translation
    errors = np.linalg.norm(predicted - r, axis=1)
    rms = float(np.sqrt(np.mean(errors ** 2)))
    maximum = float(np.max(errors))

    spread = float(np.sum((v - virtual_centre) ** 2))
    scale = float(np.sum(singular) / spread) if spread > 0.0 else float("nan")

    # A coplanar marker set has a mirror ambiguity: a reflection across its
    # plane is a proper 3-D rotation of the plane (the normal flips), so the
    # points alone cannot tell the two poses apart. Report it; the physical
    # mounting or a non-coplanar marker has to resolve it.
    _, _, plane_basis = np.linalg.svd(v - virtual_centre)
    plane_distances = np.abs((v - virtual_centre) @ plane_basis[-1])
    plane_spread = float(np.max(plane_distances))
    coplanar = plane_spread <= COPLANAR_TOLERANCE_MM

    if labels is None:
        labels = [f"P{index + 1}" for index in range(len(virtual))]
    return {
        "rotation": tuple(tuple(float(value) for value in row)
                          for row in rotation),
        "translation": tuple(float(value) for value in translation),
        "residuals": tuple(float(value) for value in errors),
        "rms": rms,
        "max": maximum,
        "scale": scale,
        "coplanar": coplanar,
        "plane_spread": plane_spread,
        "count": len(virtual),
        "labels": list(labels),
    }


def fit_registration(registration):
    """Fit the transform from one validated registration."""
    fit = fit_transform(
        [marker["virtual_mm"] for marker in registration["markers"]],
        [marker["real_mm"] for marker in registration["markers"]],
        [marker["label"] for marker in registration["markers"]],
    )
    fit["real_frame"] = registration["real_frame"]
    fit["registration_path"] = registration["path"]
    return fit


def apply_transform(fit, point):
    """Map one virtual point to the real frame."""
    try:
        x, y, z = (float(value) for value in point)
    except (TypeError, ValueError):
        raise ValueError(
            f"a point must hold three numbers, got {point!r}") from None
    rotation = fit["rotation"]
    translation = fit["translation"]
    return tuple(
        rotation[row][0] * x + rotation[row][1] * y
        + rotation[row][2] * z + translation[row]
        for row in range(3))


def fit_note(fit):
    """One readable summary of the fit, with the checks that matter."""
    lines = [
        f"{fit['count']} markers, real frame {fit['real_frame']!r}",
        f"residual {fit['rms']:.2f} mm RMS, {fit['max']:.2f} mm max; "
        f"scale {fit['scale']:.4f}",
    ]
    if fit["rms"] > MAX_RESIDUAL_MM:
        lines.append(
            f"WARNING: above {MAX_RESIDUAL_MM:.1f} mm. The real coordinates, "
            f"the virtual coordinates, or the marker labels do not match.")
    if abs(fit["scale"] - 1.0) > MAX_SCALE_ERROR:
        lines.append(
            f"WARNING: scale differs from 1 by "
            f"{abs(fit['scale'] - 1.0) * 100.0:.1f}%. The rigid fit cannot "
            f"stretch the device; check the measured distances.")
    if fit["count"] == MIN_MARKERS:
        lines.append(
            "three markers: the residual checks the triangle's shape, not a "
            "fourth point.")
    if fit.get("coplanar"):
        lines.append(
            f"markers are coplanar (max off-plane {fit['plane_spread']:.2f} "
            f"mm): a mirror flip is not detectable from the points alone. "
            f"Keep the device face the right way up, or add a non-coplanar "
            f"marker.")
    return "\n".join(lines)


def fit_payload(fit, registration):
    """The fit in a JSON-ready shape, for the solved-file record."""
    rows = []
    for marker, error in zip(registration["markers"], fit["residuals"]):
        predicted = apply_transform(fit, marker["virtual_mm"])
        rows.append({
            "label": marker["label"],
            "virtual_mm": [round(value, 3) for value in marker["virtual_mm"]],
            "real_mm": [round(value, 3) for value in marker["real_mm"]],
            "predicted_mm": [round(value, 3) for value in predicted],
            "error_mm": round(float(error), 3),
        })
    return {
        "real_frame": fit["real_frame"],
        "registration": fit["registration_path"],
        "rotation": [list(row) for row in fit["rotation"]],
        "translation": [float(value) for value in fit["translation"]],
        "rms_mm": round(float(fit["rms"]), 3),
        "max_mm": round(float(fit["max"]), 3),
        "scale": round(float(fit["scale"]), 6),
        "coplanar": bool(fit.get("coplanar", False)),
        "plane_spread_mm": round(float(fit.get("plane_spread", 0.0)), 3),
        "markers": rows,
    }
