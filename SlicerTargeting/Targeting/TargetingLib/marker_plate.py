"""Draw the marker plate in the cube, and check it against camera 1.

Slicer side of :mod:`targeting.marker_plate`: it turns the declared numbers into
scene nodes and reads the pixels of the ``MarkerClicks`` point list. The
arithmetic stays in the pure module, so the panel and the console tool agree.

Nothing here guesses: a view showing the wrong camera, a point outside the
picture, or a plate outside the cube refuses by name.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime

import slicer

import vtk

from targeting import marker_plate as config
from targeting.converter import OutOfFieldError, check_in_field_ras, cube_to_ras
from targeting.frame import Frame

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
FRAME_PATH = PROJECT_ROOT / "frame.json"
SOLVED_PATH = PROJECT_ROOT / "logs" / "marker-frame.json"

PLATE_NODE = "MarkerPlate"
OUTLINE_NODE = "MarkerPlateOutline"  # old scenes only; removed on apply
CLICKS_NODE = "MarkerClicks"
CAMERA_VIEW = "Red"
CAMERA_NAME = "c1"
CAMERA_NAMES = ("c1", "c2", "c3")
PLATE_COLOUR = (1.00, 0.82, 0.25)
GLYPH_SCALE = 3.0


def _apply4(matrix, vector):
    return tuple(sum(matrix[i][j] * vector[j] for j in range(4))
                 for i in range(4))


def _matrix4(vtk_matrix):
    return tuple(tuple(float(vtk_matrix.GetElement(i, j)) for j in range(4))
                 for i in range(4))


def _node(name, class_name):
    """The named node of the right class, replaced rather than shadowed."""
    node = slicer.mrmlScene.GetFirstNodeByName(name)
    if node is not None and not node.IsA(class_name):
        slicer.mrmlScene.RemoveNode(node)
        node = None
    if node is None:
        node = slicer.mrmlScene.AddNewNodeByClass(class_name, name)
    if node.GetDisplayNode() is None:
        node.CreateDefaultDisplayNodes()
    return node


def _cube_view_ids():
    """The view ids that are not showing a camera picture.

    A view displaying c1/c2/c3 is a photograph, not a coordinate space: drawing
    the plate there puts labels at RAS positions that mean nothing on the
    picture. Restricting the display keeps the plate in the 3D view and in the
    slice views that actually show the cube.
    """
    ids = []
    layout = slicer.app.layoutManager()
    for name in ("Red", "Green", "Yellow"):
        widget = layout.sliceWidget(name)
        if widget is None or widget.mrmlSliceNode() is None:
            continue
        volume_id = (widget.sliceLogic().GetSliceCompositeNode()
                     .GetBackgroundVolumeID())
        volume = slicer.mrmlScene.GetNodeByID(volume_id) if volume_id else None
        if volume is not None and volume.GetName() in CAMERA_NAMES:
            continue
        ids.append(widget.mrmlSliceNode().GetID())
    if hasattr(layout, "threeDWidget"):
        widget = layout.threeDWidget(0)
        if widget is not None and widget.mrmlViewNode() is not None:
            ids.append(widget.mrmlViewNode().GetID())
    return ids


def _restrict(display, view_ids):
    """Show a display node in the named views only."""
    try:
        display.SetViewNodeIDs(view_ids)
    except TypeError:  # the Slicer-API spelling difference across builds
        array = vtk.vtkStringArray()
        for view_id in view_ids:
            array.InsertNextValue(view_id)
        display.SetViewNodeIDs(array)


def _plate_ras(cube, frame):
    """Cube millimetres to RAS, refused by name when they leave the cube."""
    ras = cube_to_ras(cube, frame)
    try:
        check_in_field_ras(dict(zip(("R", "A", "S"), ras)), frame)
    except OutOfFieldError as exc:
        raise RuntimeError(
            f"the plate does not fit in the cube: {exc}") from None
    return ras


def build_plate(plate, frame=None):
    """Draw the markers at their place in the cube.

    The plate outline is not drawn: the markers are the reference, and a
    rectangle around them only adds edges to misread. A node left by an older
    scene is removed here, so a stale ring cannot linger.
    """
    frame = Frame.load(FRAME_PATH) if frame is None else frame
    view_ids = _cube_view_ids()
    node = _node(PLATE_NODE, "vtkMRMLMarkupsFiducialNode")
    node.RemoveAllControlPoints()
    display = node.GetDisplayNode()
    display.SetColor(*PLATE_COLOUR)
    display.SetSelectedColor(*PLATE_COLOUR)
    display.SetPointLabelsVisibility(True)
    display.SetGlyphScale(GLYPH_SCALE)
    display.SetVisibility(True)
    display.SetVisibility2D(True)
    _restrict(display, view_ids)

    origin = plate["origin"]
    for marker in plate["markers"]:
        ras = _plate_ras((origin[0] + marker["mm"][0],
                          origin[1] + marker["mm"][1],
                          origin[2] + marker["z"]), frame)
        index = node.AddControlPoint(ras[0], ras[1], ras[2])
        node.SetNthControlPointLabel(index, marker["label"])

    stale = slicer.mrmlScene.GetFirstNodeByName(OUTLINE_NODE)
    if stale is not None:
        slicer.mrmlScene.RemoveNode(stale)
    return node


def camera_pixels(count):
    """Camera 1's pixels for the MarkerClicks points, in list order.

    The Red view's own background volume supplies the RAS-to-pixel matrix, so
    the answer is whatever picture is actually on screen -- not a remembered
    geometry. A view showing another camera, or a point outside the picture,
    refuses by name rather than clamping.
    """
    widget = slicer.app.layoutManager().sliceWidget(CAMERA_VIEW)
    if widget is None:
        raise RuntimeError(f"there is no {CAMERA_VIEW} view in this layout")
    volume_id = (widget.sliceLogic().GetSliceCompositeNode()
                 .GetBackgroundVolumeID())
    volume = slicer.mrmlScene.GetNodeByID(volume_id) if volume_id else None
    if volume is None or volume.GetImageData() is None:
        raise RuntimeError(f"the {CAMERA_VIEW} view has no camera picture; "
                           f"start the stream first")
    if volume.GetName() != CAMERA_NAME:
        raise RuntimeError(
            f"the {CAMERA_VIEW} view is showing {volume.GetName()!r}, not "
            f"{CAMERA_NAME!r}; show camera 1 there before clicking")

    node = slicer.mrmlScene.GetFirstNodeByName(CLICKS_NODE)
    if node is None or not node.IsA("vtkMRMLMarkupsFiducialNode"):
        return None, f"no {CLICKS_NODE} point list"
    points = node.GetNumberOfControlPoints()
    if points == 0:
        return None, f"{CLICKS_NODE} is empty"
    if points != count:
        return None, (f"{CLICKS_NODE} holds {points} points but the plate has "
                      f"{count} markers")

    ras_to_ijk = vtk.vtkMatrix4x4()
    volume.GetRASToIJKMatrix(ras_to_ijk)
    matrix = _matrix4(ras_to_ijk)
    width, height = volume.GetImageData().GetDimensions()[0:2]

    pixels = []
    for index in range(points):
        ras = node.GetNthControlPointPosition(index)
        ijk = _apply4(matrix, (ras[0], ras[1], ras[2], 1.0))
        u, v = float(ijk[0]), float(ijk[1])
        if not (0.0 <= u < width and 0.0 <= v < height):
            raise RuntimeError(
                f"{CLICKS_NODE} point {index + 1} is outside the picture "
                f"({u:.0f}, {v:.0f} of {width}x{height})")
        pixels.append((u, v))
    return pixels, ""


def check(plate):
    """Fit the homography from the clicks and report the error. Optional."""
    note = config.coplanarity_note(plate)
    if note is not None:
        return {"status": "unchecked", "note": note}
    try:
        pixels, note = camera_pixels(len(plate["markers"]))
    except RuntimeError as exc:
        # No camera in the Red view is not a failure to draw the plate: it only
        # means the clicks cannot be checked yet.
        return {"status": "unchecked", "note": str(exc)}
    if pixels is None:
        return {"status": "unchecked", "note": note}

    plate_mm = [marker["mm"] for marker in plate["markers"]]
    homography = config.fit_homography(pixels, plate_mm)

    rows = []
    total = 0.0
    for marker, pixel in zip(plate["markers"], pixels):
        predicted = config.to_plate(homography, pixel[0], pixel[1])
        error = ((predicted[0] - marker["mm"][0]) ** 2
                 + (predicted[1] - marker["mm"][1]) ** 2) ** 0.5
        total += error * error
        rows.append({"label": marker["label"], "mm": marker["mm"],
                     "px": pixel, "read_mm": predicted, "error_mm": error})
    rms = (total / len(rows)) ** 0.5

    solved = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "plate": plate["path"],
        "homography": homography,
        "residual_mm": rms,
        "markers": rows,
    }
    SOLVED_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOLVED_PATH.write_text(json.dumps(solved, indent=2) + "\n", encoding="utf-8")
    solved["status"] = "checked"
    solved["path"] = str(SOLVED_PATH)
    return solved


def summary(plate, solved):
    """One readable summary of what was placed and what the clicks say."""
    origin = plate["origin"]
    lines = [f"plate {plate['width']:.1f} x {plate['height']:.1f} mm, "
             f"{len(plate['markers'])} markers, cube origin "
             f"({origin[0]:.1f}, {origin[1]:.1f}, {origin[2]:.1f}) mm"]
    for marker in plate["markers"]:
        lines.append(
            f"  {marker['label']:>4s}  plate ({marker['mm'][0]:6.1f}, "
            f"{marker['mm'][1]:6.1f}, {marker['z']:5.1f}) mm   cube "
            f"({origin[0] + marker['mm'][0]:6.1f}, "
            f"{origin[1] + marker['mm'][1]:6.1f}, "
            f"{origin[2] + marker['z']:5.1f}) mm")
    if not plate["coplanar"]:
        lines.append(f"  z spans {plate['z_spread']:.2f} mm: the markers are "
                     f"not one plane, so no pixel map was fitted")

    if solved["status"] != "checked":
        lines.append(f"clicks: not checked ({solved['note']})")
        lines.append("  place points in MarkerClicks on the marker centres, "
                     "same order")
        return "\n".join(lines)

    lines.append(f"clicks: {len(solved['markers'])} points, "
                 f"residual {solved['residual_mm']:.2f} mm RMS")
    for row in solved["markers"]:
        lines.append(
            f"  {row['label']:>4s}  pixel ({row['px'][0]:6.1f}, "
            f"{row['px'][1]:6.1f})  reads ({row['read_mm'][0]:6.1f}, "
            f"{row['read_mm'][1]:6.1f}) mm  error {row['error_mm']:.2f} mm")
    lines.append(f"  map written to {solved['path']}")
    if solved["residual_mm"] > config.MAX_RESIDUAL_MM:
        lines.append(f"  WARNING: above {config.MAX_RESIDUAL_MM:.1f} mm. The "
                     "click order, the plate size, or the plate's flatness "
                     "does not match the config.")
    elif len(solved["markers"]) == 4:
        lines.append("  four markers fit exactly: the error cannot detect a "
                     "mistake.")
    return "\n".join(lines)


def apply(plate):
    """Draw the plate, check it, and return the text the panel shows."""
    build_plate(plate)
    solved = check(plate)
    return {"plate": plate, "solved": solved, "text": summary(plate, solved)}


def run():
    """Console entry point: the file on disk, drawn and checked."""
    plate = config.load_plate()
    result = apply(plate)
    print(result["text"])
    slicer.app.processEvents()
    return result
