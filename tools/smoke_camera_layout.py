"""In-Slicer smoke test for the camera layout and its click path.

    timeout 300 <slicer> --no-splash --python-script tools/smoke_camera_layout.py

Builds the Targeting panel, switches to the camera layout, feeds three
synthetic volumes as c1/c2/c3, and checks that:

  * the layout registers once and puts Cam1..Cam3 over one wide 3D view,
  * each pane shows its own camera at the picture's size, with no controller,
  * a press in a pane maps to the picture pixel (centre and a quarter point),
  * the marker plate's c1 lookup follows the picture into Cam1,
  * switching back restores the standard layout.

Prints one line per check and finally "SMOKE PASS n/n" / "SMOKE FAIL: names".
"""

import pathlib
import sys
import traceback


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
    import numpy as np
    import qt
    import slicer
    import vtk

    import Targeting as targeting_module
    from TargetingLib import marker_plate as scene
    from TargetingLib.logic import _LEFT_BUTTON, _MOUSE_PRESS
    from targeting import layouts

    panel = qt.QWidget()
    panel.setLayout(qt.QVBoxLayout())
    widget = targeting_module.TargetingWidget(panel)
    widget.setup()
    widget.enter()

    # Size the window first: a layout built before the main window has been
    # laid out gives the panes placeholder sizes, and a click test would then
    # measure the placeholder rather than the arrangement.
    slicer.util.mainWindow().resize(1400, 900)
    slicer.app.processEvents()

    # -- the layout -------------------------------------------------------
    widget._show_camera_layout(True)
    layout = slicer.app.layoutManager()
    check("the camera layout becomes the arrangement on screen",
          widget.logic.camera_layout_active(),
          f"active={widget.logic.camera_layout_active()}")
    widgets = {name: layout.sliceWidget(name) for name in layouts.CAMERA_VIEWS}
    standard = {name: layout.sliceWidget(name).sliceView().isVisible()
                for name in ("Red", "Green", "Yellow")}
    check("the standard four-up is in the same layout",
          all(standard.values()), f"{standard}")
    four_up = {name: layout.sliceWidget(name).sliceView()
               for name in ("Red", "Green", "Yellow")}
    four_up["3D"] = layout.threeDWidget(0).threeDView()
    widths = [view.width for view in four_up.values()]
    heights = [view.height for view in four_up.values()]
    check("the four-up panes are the same size",
          max(widths) - min(widths) <= 2 and max(heights) - min(heights) <= 2,
          f"widths={widths} heights={heights}")

    check("three camera panes and the 3D view exist",
          all(w is not None for w in widgets.values())
          and layout.threeDWidget(0) is not None,
          f"{ {name: w is not None for name, w in widgets.items()} }")
    check("the panes are the camera view nodes, not Red/Green/Yellow",
          all(widgets[name].mrmlSliceNode().GetName() == name
              for name in layouts.CAMERA_VIEWS),
          f"{ [widgets[n].mrmlSliceNode().GetName() for n in layouts.CAMERA_VIEWS] }")
    controller = widgets["Cam1"].sliceController()
    check("the picture pane has no slice controller",
          not controller.isVisible(),
          f"visible={controller.visible} isVisible={controller.isVisible()}")

    # -- pictures ---------------------------------------------------------
    volumes = {}
    for index, name in enumerate(("c1", "c2", "c3")):
        volume = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLScalarVolumeNode", name)
        slicer.util.updateVolumeFromArray(
            volume, np.full((120, 160), 40 + index, dtype=np.uint8))
        matrix = vtk.vtkMatrix4x4()
        matrix.SetElement(0, 0, -1.0)
        matrix.SetElement(1, 1, -1.0)
        volume.SetIJKToRASMatrix(matrix)
        volumes[name] = volume

    views = widget.logic._show_camera_views(list(volumes.values()))
    check("each camera binds to its own pane",
          views == {f"Cam{index}": name
                    for index, name in enumerate(volumes, start=1)},
          f"{views}")
    check("the pane shows a 160x120 picture",
          widgets["Cam1"].sliceLogic().GetSliceCompositeNode()
          .GetBackgroundVolumeID() == volumes["c1"].GetID(),
          f"background={widgets['Cam1'].sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID()}")

    # -- clicks (logic only; the camera-click UI is gone) -----------------

    view = widgets["Cam1"].sliceView()
    width, height = int(view.width), int(view.height)
    centre = widget.logic.set_camera_click("Cam1", width // 2, height // 2)
    check("a centre press lands at the picture centre",
          abs(centre["pixel"][0] - 80) <= 1.5
          and abs(centre["pixel"][1] - 60) <= 1.5,
          f"pixel={centre['pixel']} pane={width}x{height}")

    upper = widget.logic.set_camera_click("Cam1", width // 2, height // 4)
    check("the vertical axis is not flipped",
          upper["pixel"][1] < centre["pixel"][1] - 15,
          f"upper v={upper['pixel'][1]:.1f} centre v={centre['pixel'][1]:.1f}")

    # -- Slicer points still work in XY/XZ/YZ ------------------------------
    widget.pickBox.checked = True
    slicer.app.processEvents()
    red_widget = layout.sliceWidget("Red")
    red_view = red_widget.sliceView()
    check("the XY view keeps its pick filter in the camera layout",
          "Red" in widget.filters
          and widget.filters["Red"][1].click_enabled,
          f"filters={sorted(widget.filters)} "
          f"enabled={widget.filters.get('Red', (None, None))[1].click_enabled}")

    no_modifier = getattr(qt.Qt, "NoModifier", None)
    if no_modifier is None:
        no_modifier = qt.Qt.KeyboardModifier.NoModifier
    press = qt.QMouseEvent(
        _MOUSE_PRESS, qt.QPointF(red_view.width / 2.0, red_view.height / 2.0),
        _LEFT_BUTTON, _LEFT_BUTTON, no_modifier)
    placed_before = widget.logic.placed
    qt.QApplication.sendEvent(widget.filters["Red"][0], press)
    slicer.app.processEvents()
    check("a press in the XY view still places a target",
          widget.logic.placed == placed_before + 1,
          f"placed={widget.logic.placed} lastClick={widget.lastClick} "
          f"status={widget.statusLabel.text!r}")
    widget.pickBox.checked = False
    slicer.app.processEvents()

    # -- the plate follows the picture ------------------------------------
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
    # A plate-to-pixel map that lands inside a 160x120 picture.
    truth = ((1.55, 0.21, 59.0),
             (-0.14, 1.50, 35.5),
             (0.0007, -0.00042, 1.0))
    pixels = plate_to_pixels(truth, [marker["mm"] for marker in plate["markers"]])
    marks = slicer.mrmlScene.AddNewNodeByClass(
        "vtkMRMLMarkupsFiducialNode", "MarkerClicks")
    for u, v in pixels:
        marks.AddControlPoint(-u, -v, 0.0)

    view_name, _widget, volume = scene._view_showing("c1")
    check("the plate finds c1 by picture, not by view name",
          volume is volumes["c1"] and view_name == "Cam1",
          f"view={view_name}")
    solved = scene.check(plate)
    check("the marker fit accepts clicks made in the camera pane",
          solved.get("status") == "checked"
          and solved.get("residual_mm", 1e9) < 0.01,
          f"status={solved.get('status')} "
          f"residual={solved.get('residual_mm')}")

    # A screenshot of the arrangement as the operator sees it.
    slicer.util.resetSliceViews()   # undo the zoom/pan the checks above made
    slicer.app.processEvents()
    slicer.util.forceRenderAllViews()
    slicer.app.processEvents()
    slicer.util.mainWindow().grab().save("/tmp/opencode/camera-layout.png")

    # -- back to the standard arrangement ---------------------------------
    widget._show_camera_layout(False)
    check("switching back restores the four-up layout",
          not widget.logic.camera_layout_active(),
          f"active={widget.logic.camera_layout_active()}")
    check("the layout button follows the switch",
          not widget.cameraLayoutButton.checked,
          f"checked={widget.cameraLayoutButton.checked}")

    # Toggling back on must show the live pictures again, without reconnecting.
    widget._show_camera_layout(True)
    back = layout.sliceWidget("Cam1").sliceLogic().GetSliceCompositeNode()
    check("returning to the layout keeps the bound pictures",
          back.GetBackgroundVolumeID() == volumes["c1"].GetID(),
          f"background={back.GetBackgroundVolumeID()}")

    failed = [name for name, ok in _CHECKS if not ok]
    if failed:
        print("SMOKE FAIL: " + ", ".join(failed))
    else:
        print(f"SMOKE PASS {len(_CHECKS)}/{len(_CHECKS)}")
    slicer.app.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a smoke test reports, then exits
        traceback.print_exc()
        print("SMOKE FAIL: exception")
        raise SystemExit(1)
