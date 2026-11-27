"""Headless check that tools/load_markers.py places the plate and fits the map.

Runs in a separate Slicer:

    xvfb-run -a timeout 300 <slicer> --no-splash \
        --python-script tools/smoke_load_markers.py

Builds the scene the tool expects -- a ``c1`` volume in the Red view and a
``MarkerClicks`` list at pixels produced by a known plate-to-pixel map -- runs
the tool the way the console does, and checks what appeared. Prints one line
per check and finally "SMOKE PASS n/n" or "SMOKE FAIL: names".
"""

import pathlib
import sys
import traceback

# No hardcoded fallback: panic instead of guessing (see make_field_cube).


def _resolve_project_root(_globals=None, _env=None):
    """Checkout root without scanning the disk (same rule as make_field_cube)."""
    import os

    g = _globals if _globals is not None else globals()
    e = _env if _env is not None else os.environ
    override = g.get("PROJECT_ROOT_OVERRIDE")
    if override:
        return pathlib.Path(override)
    env = e.get("HIMX_CAM_ROOT")
    if env:
        return pathlib.Path(env)
    try:
        here = pathlib.Path(__file__).resolve()
    except NameError:
        here = None
    if here is not None and here.is_file():
        for parent in [here.parent, *here.parents]:
            if (parent / "frame.json").is_file() and (
                parent / "targeting" / "converter.py"
            ).is_file():
                return parent
    raise RuntimeError(
        "could not locate himx-cam checkout (no fallback): set HIMX_CAM_ROOT "
        f"to the directory containing frame.json. __file__={here!r}"
    )


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "SlicerTargeting" / "Targeting") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "SlicerTargeting" / "Targeting"))

_CHECKS = []


def check(name, ok, detail=""):
    _CHECKS.append((name, bool(ok)))
    print(f"CHECK {name}: {'ok' if ok else 'FAIL'} {detail}")


def plate_to_pixels(map_matrix, plate_mm):
    """Where plate millimetres land, from a known plate-to-pixel map."""
    rows = []
    for x, y in plate_mm:
        w = map_matrix[2][0] * x + map_matrix[2][1] * y + map_matrix[2][2]
        rows.append(((map_matrix[0][0] * x + map_matrix[0][1] * y
                      + map_matrix[0][2]) / w,
                     (map_matrix[1][0] * x + map_matrix[1][1] * y
                      + map_matrix[1][2]) / w))
    return rows


def main():
    import qt
    import slicer
    import vtk

    from TargetingLib import marker_plate as scene
    from targeting.converter import cube_to_ras
    from targeting.frame import Frame
    from targeting import marker_plate as config

    frame = Frame.load(PROJECT_ROOT / "frame.json")

    # A picture the size the stream sends, placed the way the sender places it:
    # IJKToRAS = diag(-1, -1, 1, 1), so RAS (-u, -v, 0) is pixel (u, v).
    import numpy as np

    volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "c1")
    slicer.util.updateVolumeFromArray(
        volume, np.full((320, 320), 40, dtype=np.uint8))
    matrix = vtk.vtkMatrix4x4()
    matrix.SetElement(0, 0, -1.0)
    matrix.SetElement(1, 1, -1.0)
    volume.SetIJKToRASMatrix(matrix)

    layout = slicer.app.layoutManager()
    layout.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
    widget = layout.sliceWidget("Red")
    widget.sliceLogic().GetSliceCompositeNode().SetBackgroundVolumeID(volume.GetID())
    slicer.util.resetSliceViews()
    slicer.app.processEvents()

    # The tool's config, and a perspective map whose plate-to-pixel direction is
    # known exactly. The fit must recover it from the clicks alone.
    plate = {
        "width": 60.0, "height": 40.0,
        "markers": [
            {"label": "M1", "mm": (0.0, 0.0), "z": 0.0},
            {"label": "M2", "mm": (60.0, 0.0), "z": 0.0},
            {"label": "M3", "mm": (60.0, 40.0), "z": 0.0},
            {"label": "M4", "mm": (0.0, 40.0), "z": 0.0},
            {"label": "M5", "mm": (30.0, 20.0), "z": 0.0},
        ],
        "origin": (20.0, 30.0, 0.0),
        "path": str(PROJECT_ROOT / "marker-plate.json"),
        "z_spread": 0.0,
        "coplanar": True,
    }
    truth = ((3.10, 0.42, 118.0),
             (-0.28, 3.00, 71.0),
             (0.00035, -0.00021, 1.0))
    pixels = plate_to_pixels(truth, [marker["mm"] for marker in plate["markers"]])

    marks = slicer.mrmlScene.AddNewNodeByClass(
        "vtkMRMLMarkupsFiducialNode", "MarkerClicks")
    for u, v in pixels:
        marks.AddControlPoint(-u, -v, 0.0)

    # A ring left by an older scene must not survive an apply.
    slicer.mrmlScene.AddNewNodeByClass(
        "vtkMRMLMarkupsClosedCurveNode", "MarkerPlateOutline")

    # Run the tool the way the console does: exec() in a prepared namespace.
    source = (PROJECT_ROOT / "tools" / "load_markers.py").read_text(encoding="utf-8")
    namespace = {"__name__": "console",
                 "PROJECT_ROOT_OVERRIDE": str(PROJECT_ROOT)}
    exec(compile(source, str(PROJECT_ROOT / "tools" / "load_markers.py"), "exec"),
         namespace)

    node = slicer.mrmlScene.GetFirstNodeByName("MarkerPlate")
    check("MarkerPlate exists", node is not None)
    check("five markers placed",
          node is not None and node.GetNumberOfControlPoints() == 5,
          f"points={node.GetNumberOfControlPoints() if node else None}")
    check("markers labelled M1..M5",
          node is not None and [node.GetNthControlPointLabel(i)
                                for i in range(node.GetNumberOfControlPoints())]
          == ["M1", "M2", "M3", "M4", "M5"])

    check("no ring is drawn",
          slicer.mrmlScene.GetFirstNodeByName("MarkerPlateOutline") is None)

    plate_display = node.GetDisplayNode() if node is not None else None
    check("plate markers are locked and non-selectable",
          node is not None and node.GetLocked()
          and not node.GetSelectable()
          and plate_display is not None
          and not plate_display.GetSelectable(),
          f"locked={None if node is None else node.GetLocked()}")
    check("plate handles are not interactive",
          plate_display is not None
          and not plate_display.GetHandlesInteractive())
    check("plate colour is orange, not the target blue",
          plate_display is not None
          and tuple(round(value, 3) for value in plate_display.GetColor())
          == tuple(round(value, 3) for value in scene.PLATE_COLOUR)
          and tuple(scene.PLATE_COLOUR) != (0.2, 0.4, 1.0),
          f"colour={None if plate_display is None else plate_display.GetColor()}")

    expected = [cube_to_ras((plate["origin"][0] + marker["mm"][0],
                             plate["origin"][1] + marker["mm"][1],
                             plate["origin"][2] + marker["z"]), frame)
                for marker in plate["markers"]]
    placed = [node.GetNthControlPointPosition(i)
              for i in range(node.GetNumberOfControlPoints())]
    worst = max(max(abs(placed[i][j] - expected[i][j]) for j in range(3))
                for i in range(len(expected)))
    check("markers sit at the configured cube position", worst < 1e-6,
          f"worst axis error {worst:.2e} mm")

    # An optional z is added to the origin, not ignored.
    raised = {
        "width": plate["width"], "height": plate["height"],
        "origin": plate["origin"], "path": plate["path"],
        "z_spread": 5.0, "coplanar": False,
        "markers": [dict(marker) for marker in plate["markers"]],
    }
    raised["markers"][4]["z"] = 5.0
    scene.build_plate(raised, frame)
    node = slicer.mrmlScene.GetFirstNodeByName("MarkerPlate")
    lifted = node.GetNthControlPointPosition(4)
    check("a marker z lifts the point", abs(lifted[2] - (expected[4][2] + 5.0)) < 1e-6,
          f"z={lifted[2]:.2f} expected {expected[4][2] + 5.0:.2f}")
    scene.build_plate(plate, frame)

    solved = scene.check(config.load_plate())
    check("the click fit is accepted", solved["status"] == "checked",
          str(solved.get("note", "")))
    check("the fit recovers the clicks", solved.get("residual_mm", 99.0) < 1e-6,
          f"residual {solved.get('residual_mm')} mm")
    check("the map is written",
          pathlib.Path(solved.get("path", "")).is_file())

    # A short click list must be reported, not guessed.
    marks.RemoveNthControlPoint(4)
    short = scene.check(config.load_plate())
    check("a short click list is refused by name",
          short["status"] == "unchecked" and "4 points" in short["note"],
          short.get("note", ""))

    # A plate pushed outside the cube must refuse rather than draw half of it.
    outside = dict(plate)
    outside["origin"] = (60.0, 60.0, 0.0)
    try:
        scene.build_plate(outside, frame)
        check("a plate outside the cube is refused", False, "no error raised")
    except RuntimeError as exc:
        check("a plate outside the cube is refused", "does not fit" in str(exc),
              str(exc))
    scene.build_plate(plate, frame)

    # -- the panel in the Targeting module ----------------------------------
    # The panel is the part the operator actually uses; a working module
    # underneath is not enough if the table cannot be read or saved.
    import Targeting as targeting_module

    panel = qt.QWidget()
    panel.setLayout(qt.QVBoxLayout())
    widget = targeting_module.TargetingWidget(panel)
    widget.setup()
    widget.enter()

    check("the panel loads the plate file",
          widget.markerTable.rowCount == len(plate["markers"]),
          f"rows={widget.markerTable.rowCount}")
    check("the panel shows the labels",
          [widget.markerTable.item(row, 0).text()
           for row in range(widget.markerTable.rowCount)]
          == ["M1", "M2", "M3", "M4", "M5"])

    widget.markerApplyButton.click()
    check("Apply draws the plate",
          slicer.mrmlScene.GetFirstNodeByName("MarkerPlate") is not None)
    check("Apply reports the check in the panel",
          "residual" in widget.markerStatusLabel.text
          or "not checked" in widget.markerStatusLabel.text,
          widget.markerStatusLabel.text.splitlines()[0]
          if widget.markerStatusLabel.text else "")

    widget.markerTable.item(1, 1).setText("not-a-number")
    widget.markerApplyButton.click()
    check("a bad cell is refused by name",
          widget.markerStatusLabel.text.startswith("markers:")
          and "not a number" in widget.markerStatusLabel.text,
          widget.markerStatusLabel.text)
    widget.markerTable.item(1, 1).setText("60")

    before = scene.config.DEFAULT_PATH.stat().st_mtime_ns
    widget.onMarkerSave()
    after = scene.config.DEFAULT_PATH.stat().st_mtime_ns
    check("Save writes the file", after != before,
          widget.markerStatusLabel.text)

    panel.resize(430, 1100)
    panel.adjustSize()
    panel.grab().save("/tmp/opencode/marker-panel.png")

    failed = [name for name, ok in _CHECKS if not ok]
    if failed:
        print("SMOKE FAIL: " + ", ".join(failed))
    else:
        print(f"SMOKE PASS {len(_CHECKS)}/{len(_CHECKS)}")
    pathlib.Path("/tmp/opencode").mkdir(parents=True, exist_ok=True)
    slicer.util.mainWindow().grab().save("/tmp/opencode/load-markers.png")
    slicer.app.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("SMOKE FAIL: exception")
        sys.exit(1)
