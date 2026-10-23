"""Slicer smoke: three pictures land in three views, and nothing else changes.

    uv run tools/camera_sandbox.py --write-frames /tmp/opencode/frames
    timeout 300 <slicer> --no-splash --python-script tools/smoke_camera_views.py
"""

import os
import pathlib
import sys
import time
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

_CHECKS = []


def check(name, ok, detail=""):
    _CHECKS.append((name, bool(ok)))
    print(f"CHECK {name}: {'ok' if ok else 'FAIL'} {detail}")


def _outside_is_refused(logic):
    try:
        logic.set_camera_click("Red", 2.0, 2.0)
    except ValueError as exc:
        return "Red" in str(exc)
    return False


def main():
    import slicer
    module_dir = PROJECT_ROOT / "SlicerTargeting" / "Targeting"
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))
    module_dir = PROJECT_ROOT / "SlicerTargeting" / "Targeting"
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))
    from TargetingLib.logic import (
        CAMERA_CLICKS_NODE_NAME,
        CAMERA_CLICKS_NODE_NAMES,
        CAMERA_CLICK_COLOURS,
        STREAM_FRUSTUM_NODE_NAMES,
        STREAM_ORIGIN_NODE_NAMES,
        TargetingLogic,
    )
    from targeting.clicks import read_clicks

    def _click_nodes():
        return [slicer.mrmlScene.GetFirstNodeByName(name)
                for name in CAMERA_CLICKS_NODE_NAMES]

    def _click_points():
        return [node.GetNumberOfControlPoints() if node is not None else 0
                for node in _click_nodes()]

    def _display_view_ids(node):
        """The display node's restricted view list, across Slicer spellings."""
        display = node.GetDisplayNode()
        if display is None:
            return None
        for method in ("GetNthViewNodeID", "GetViewNodeID"):
            try:
                count = display.GetNumberOfViewNodeIDs()
                getter = getattr(display, method)
                return [getter(index) for index in range(count)]
            except Exception:
                continue
        try:
            array = display.GetViewNodeIDs()
            if array is not None:
                return [array.GetValue(index)
                        for index in range(array.GetNumberOfValues())]
        except Exception:
            pass
        return None

    folder = pathlib.Path(os.environ["HIMX_TEST_FRAMES"])
    logic = TargetingLogic()

    result = logic.load_camera_views(folder)
    check("three views loaded", result["status"] == "loaded" and len(result["views"]) == 3,
          str(result.get("problems", ())))
    positions = [slicer.mrmlScene.GetFirstNodeByName(name)
                 for name in (*STREAM_FRUSTUM_NODE_NAMES,
                              *STREAM_ORIGIN_NODE_NAMES)]
    check("camera positions are shown for a calibrated frame set",
          all(node is not None for node in positions),
          str([node.GetName() if node else None for node in positions]))
    for widget_name, node_name in result["views"].items():
        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        background = widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID()
        node = slicer.mrmlScene.GetNodeByID(background)
        check(f"{widget_name} shows {node_name}",
              node is not None and node.GetName() == node_name)
        check(f"{widget_name} is axial",
              abs(widget.mrmlSliceNode().GetSliceToRAS().GetElement(0, 2)) < 1e-9)
        check(f"{widget_name} slice plane is hidden in 3D",
              not widget.mrmlSliceNode().GetSliceVisible())
    empty = logic.load_camera_views(pathlib.Path("/tmp/opencode/no-frames-here"))
    check("a folder with no manifest is reported, not crashed",
          empty["status"] == "nothing")

    widget = slicer.app.layoutManager().sliceWidget("Red")
    view = widget.sliceView()
    size = view.size
    centre = (size.width() / 2.0, size.height() / 2.0)

    logic.set_camera_click("Red", *centre)
    u, v = logic.camera_pixel("Red", *centre)
    check("a centre click is the picture centre",
          abs(u - 160.0) < 6.0 and abs(v - 160.0) < 6.0, f"pixel=({u:.1f}, {v:.1f})")

    logic.clear_camera_clicks()
    logic.set_camera_click("Red", centre[0] + size.width() * 0.2, centre[1])
    right_u, _ = logic.camera_pixel("Red", centre[0] + size.width() * 0.2, centre[1])
    check("a click to the right moves u right", right_u > 160.0 + 30.0, f"u={right_u:.1f}")

    logic.clear_camera_clicks()
    logic.set_camera_click("Red", centre[0], centre[1] + size.height() * 0.2)
    _, down_v = logic.camera_pixel("Red", centre[0], centre[1] + size.height() * 0.2)
    check("a click below moves v down", down_v > 160.0 + 30.0, f"v={down_v:.1f}")

    logic.clear_camera_clicks()
    red_xy = centre
    green_size = slicer.app.layoutManager().sliceWidget("Green").sliceView().size
    green_xy = (green_size.width() / 2.0 + green_size.width() * 0.2,
                green_size.height() / 2.0)
    yellow_size = slicer.app.layoutManager().sliceWidget("Yellow").sliceView().size
    yellow_xy = (yellow_size.width() / 2.0,
                 yellow_size.height() / 2.0 + yellow_size.height() * 0.2)

    first = logic.set_camera_click("Red", *red_xy)
    check("a single click publishes", first["published"] is not None,
          str(first["published"]))
    if first["published"] is not None:
        single, single_note = read_clicks(first["published"])
    else:
        single, single_note = None, "nothing published"
    red_u, red_v = logic.camera_pixel("Red", *red_xy)
    if single is None:
        check("one click publishes Red with the rest null", False, single_note)
    else:
        got = single.pixels[0]
        check("one click publishes Red with the rest null",
              got is not None and abs(got[0] - red_u) < 6.0
              and abs(got[1] - red_v) < 6.0
              and single.pixels[1] is None and single.pixels[2] is None,
              f"pixels={single.pixels} Red=({red_u:.1f}, {red_v:.1f})")
    click_nodes = _click_nodes()
    check("one click leaves one mark in its own node, the rest empty",
          click_nodes[0] is not None
          and click_nodes[0].GetNumberOfControlPoints() == 1
          and all(node is None or node.GetNumberOfControlPoints() == 0
                  for node in click_nodes[1:]),
          f"points={_click_points()}")

    second = logic.set_camera_click("Green", *green_xy)
    if second["published"] is not None:
        partial, partial_note = read_clicks(second["published"])
    else:
        partial, partial_note = None, "nothing published"
    if partial is None:
        check("two clicks publish a partial set", False, partial_note)
    else:
        green_u, green_v = logic.camera_pixel("Green", *green_xy)
        red = partial.pixels[0]
        green = partial.pixels[1]
        check("two clicks publish a partial set",
              red is not None and abs(red[0] - red_u) < 6.0
              and abs(red[1] - red_v) < 6.0
              and green is not None and abs(green[0] - green_u) < 6.0
              and abs(green[1] - green_v) < 6.0
              and partial.pixels[2] is None,
              f"pixels={partial.pixels}")
    third = logic.set_camera_click("Yellow", *yellow_xy)
    if third["published"] is not None:
        clicks, note = read_clicks(third["published"])
    else:
        clicks, note = None, "nothing published"
    check("three clicks publish one readable set", clicks is not None, note)
    expected = (("Red", red_xy), ("Green", green_xy), ("Yellow", yellow_xy))
    for index, (widget_name, xy) in enumerate(expected):
        u, v = logic.camera_pixel(widget_name, *xy)
        if clicks is None:
            check(f"pixel {index} is {widget_name}'s click", False, "no clicks published")
        else:
            got = clicks.pixels[index]
            check(f"pixel {index} is {widget_name}'s click",
                  got is not None
                  and abs(got[0] - u) < 6.0 and abs(got[1] - v) < 6.0,
                  f"published=({got[0]:.1f}, {got[1]:.1f}) "
                  f"{widget_name}=({u:.1f}, {v:.1f})"
                  if got is not None else "missing pixel")
    check("outside clicks are refused by view name", _outside_is_refused(logic))

    click_nodes = _click_nodes()
    check("three clicks leave one mark in each camera's node",
          all(node is not None and node.GetNumberOfControlPoints() == 1
              for node in click_nodes),
          f"points={_click_points()}")
    for index, (widget_name, xy) in enumerate(expected):
        node = click_nodes[index]
        u, v = logic.camera_pixel(widget_name, *xy)
        want_ras = logic.pixel_to_camera_ras(widget_name, u, v)
        pos = [0.0, 0.0, 0.0]
        if node is not None:
            node.GetNthControlPointPosition(0, pos)
        ras_ok = (node is not None
                  and max(abs(pos[k] - want_ras[k]) for k in range(3)) < 1e-6)
        check(f"click node {('c1', 'c2', 'c3')[index]} sits at its view's RAS",
              ras_ok,
              f"mark={tuple(round(p, 3) for p in pos)} "
              f"want={tuple(round(p, 3) for p in want_ras)}")
    for index, widget_name in enumerate(("Red", "Green", "Yellow")):
        tag = ("c1", "c2", "c3")[index]
        node = click_nodes[index]
        own = (slicer.app.layoutManager().sliceWidget(widget_name)
               .mrmlSliceNode().GetID())
        ids = _display_view_ids(node) if node is not None else None
        check(f"click {tag} shows only in its own view",
              ids == [own],
              f"node={CAMERA_CLICKS_NODE_NAMES[index]} views={ids} own={own}")
    for index, widget_name in enumerate(("Red", "Green", "Yellow")):
        tag = ("c1", "c2", "c3")[index]
        node = click_nodes[index]
        colour = (tuple(round(c, 3) for c in node.GetDisplayNode().GetColor())
                  if node is not None and node.GetDisplayNode() is not None
                  else None)
        want = tuple(round(c, 3) for c in CAMERA_CLICK_COLOURS[index])
        check(f"click {tag} wears its camera's colour",
              colour == want,
              f"colour={colour} want={want}")
        selected = (
            tuple(round(c, 3) for c in node.GetDisplayNode().GetSelectedColor())
            if node is not None and node.GetDisplayNode() is not None
            else None)
        check(f"click {tag} keeps its camera's colour when selected",
              selected == want,
              f"selected={selected} want={want}")

    guide_rays = [
        slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamClickRayC{index}")
        for index in (1, 2, 3)
    ]
    guide_lines = [
        slicer.mrmlScene.GetFirstNodeByName(
            f"CameraStreamEpipolarC{target}FromC{source}")
        for source in (1, 2, 3) for target in (1, 2, 3)
        if source != target
    ]
    check("three happy-path guide rays exist",
          all(node is not None and node.IsA("vtkMRMLModelNode")
              and node.GetPolyData() is not None
              and node.GetPolyData().GetNumberOfPoints() == 2
              for node in guide_rays),
          f"rays={[node.GetPolyData().GetNumberOfPoints()
                   if node is not None and node.GetPolyData() else None
                   for node in guide_rays]}")
    check("six happy-path epipolar lines exist",
          all(node is not None and node.GetNumberOfControlPoints() == 2
              for node in guide_lines),
          f"lines={[node.GetNumberOfControlPoints() if node is not None else None
                    for node in guide_lines]}")
    guide_image = PROJECT_ROOT / "logs" / "camera-views-guide.png"
    try:
        slicer.app.processEvents()
        slicer.util.mainWindow().grab().save(str(guide_image))
        check("the three-camera guide is captured",
              guide_image.is_file(), str(guide_image))
    except Exception as exc:  # noqa: BLE001 - evidence, not the test
        check("the three-camera guide is captured", False, str(exc))

    red_node = click_nodes[0]
    red_label = red_node.GetNthControlPointLabel(0) if red_node is not None else ""
    red_pub = clicks.pixels[0] if clicks is not None else None
    check("click label shows its pixel",
          red_node is not None and red_pub is not None
          and red_label.startswith("c1")
          and f"({red_pub[0]:.0f}, {red_pub[1]:.0f})" in red_label,
          f"label={red_label!r} pixel={red_pub}")

    pos = [0.0, 0.0, 0.0]
    click_nodes[0].GetNthControlPointPosition(0, pos)
    moved_ras = (float(pos[0]) + 10.0, float(pos[1]), float(pos[2]))
    expected = logic.ras_to_camera_pixel("Red", moved_ras)
    green_pos = [0.0, 0.0, 0.0]
    click_nodes[1].GetNthControlPointPosition(0, green_pos)
    green_ras = tuple(float(value) for value in green_pos)
    click_nodes[0].SetNthControlPointPosition(0, *moved_ras)
    edited = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.1)
        current, _ = read_clicks(logic.last_clicks_path)
        if (current is not None and current.pixels[0] is not None
                and abs(current.pixels[0][0] - expected[0]) < 6.0
                and abs(current.pixels[0][1] - expected[1]) < 6.0):
            edited = current
            break
    check("edited RAS republishes the edited pixel", edited is not None,
          f"expected=({expected[0]:.1f}, {expected[1]:.1f}) "
          f"got={(edited.pixels[0] if edited is not None else None)}")

    click_nodes[1].SetNthControlPointPosition(0, 1e6, 1e6, 1e6)
    nulled = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.1)
        current, _ = read_clicks(logic.last_clicks_path)
        if current is not None and current.pixels[1] is None:
            nulled = current
            break
    check("out-of-bounds edit publishes null for that view", nulled is not None,
          f"pixels={(nulled.pixels if nulled is not None else None)}")
    outsider_label = click_nodes[1].GetNthControlPointLabel(0)
    check("out-of-bounds label notes outside",
          "outside the picture" in outsider_label and outsider_label.startswith("c2"),
          f"label={outsider_label!r}")
    status = logic.camera_status()
    check("out-of-bounds panel notes outside",
          "c2 outside the picture" in status and "clicked 2 of 3" in status,
          f"status={status!r}")

    click_nodes[1].SetNthControlPointPosition(0, *green_ras)
    restored = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.1)
        current, _ = read_clicks(logic.last_clicks_path)
        if current is not None and current.pixels[1] is not None:
            restored = current
            break
    check("fixing the number restores the view's pixel", restored is not None,
          f"pixels={(restored.pixels if restored is not None else None)}")

    import qt

    import Targeting as targeting_module

    panel = qt.QWidget()
    panel.setLayout(qt.QVBoxLayout())
    widget = targeting_module.TargetingWidget(panel)
    widget.setup()
    widget.logic = logic          # the clicks above are this widget's state
    widget._refresh_camera_pixel_fields()

    current, _ = read_clicks(logic.last_clicks_path)
    rows_ok, rows_detail = current is not None, ""
    if current is not None:
        for index, widget_name in enumerate(("Red", "Green", "Yellow")):
            u_edit, v_edit = widget.cameraPixelEdits[widget_name]
            published = current.pixels[index]
            try:
                shown = (float(u_edit.text.strip()), float(v_edit.text.strip()))
            except ValueError:
                rows_ok, rows_detail = False, f"{widget_name} fields not numeric"
                break
            if (published is None or abs(shown[0] - published[0]) > 0.11
                    or abs(shown[1] - published[1]) > 0.11):
                rows_ok, rows_detail = False, (
                    f"{widget_name} shows {shown}, published {published}")
                break
            rows_detail = f"c1={current.pixels[0]} c2={current.pixels[1]}"
    check("panel rows show the published pixels", rows_ok, rows_detail)

    before_typing, _ = read_clicks(logic.last_clicks_path)
    green_edits = widget.cameraPixelEdits["Green"]
    green_edits[0].setText("196.5")
    green_edits[1].setText("129.0")
    typed = widget.on_camera_pixel_edited("Green")
    import vtk

    green_widget = slicer.app.layoutManager().sliceWidget("Green")
    green_volume = slicer.mrmlScene.GetNodeByID(
        green_widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID())
    matrix = vtk.vtkMatrix4x4()
    green_volume.GetIJKToRASMatrix(matrix)
    expected_ras = tuple(
        sum(matrix.GetElement(row, column) * value
            for column, value in enumerate((196.5, 129.0, 0.0, 1.0)))
        for row in range(3))
    c2_pos = [0.0, 0.0, 0.0]
    click_nodes[1].GetNthControlPointPosition(0, c2_pos)
    ras_ok = (typed is not None and typed.get("status") != "refused"
              and max(abs(c2_pos[i] - expected_ras[i]) for i in range(3)) < 1e-6)
    check("typed c2 pixel lands on the volume-matrix RAS", ras_ok,
          f"mark={tuple(round(p, 3) for p in c2_pos)} "
          f"matrix={tuple(round(p, 3) for p in expected_ras)}")

    typed_file = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.1)
        current, _ = read_clicks(logic.last_clicks_path)
        if (current is not None and current.pixels[1] is not None
                and abs(current.pixels[1][0] - 196.5) < 1e-9
                and abs(current.pixels[1][1] - 129.0) < 1e-9):
            typed_file = current
            break
    file_ok = (typed_file is not None and before_typing is not None
               and all(abs(typed_file.pixels[i][j] - before_typing.pixels[i][j]) < 1e-9
                       for i in (0, 2) for j in (0, 1)))
    check("typed c2 pixel publishes with c1/c3 unchanged", file_ok,
          f"pixels={(typed_file.pixels if typed_file is not None else None)}")

    node_before = []
    for node in _click_nodes():
        position = [0.0, 0.0, 0.0]
        if node is not None and node.GetNumberOfControlPoints() > 0:
            node.GetNthControlPointPosition(0, position)
        node_before.append(tuple(position))
    file_before, _ = read_clicks(logic.last_clicks_path)
    green_edits[0].setText("500")
    green_edits[1].setText("129.0")
    refused = widget.on_camera_pixel_edited("Green")
    slicer.app.processEvents()
    time.sleep(0.4)
    slicer.app.processEvents()
    node_same = True
    for node, before in zip(_click_nodes(), node_before):
        position = [0.0, 0.0, 0.0]
        if node is not None and node.GetNumberOfControlPoints() > 0:
            node.GetNthControlPointPosition(0, position)
        if max(abs(position[k] - before[k]) for k in range(3)) > 1e-9:
            node_same = False
    file_after, _ = read_clicks(logic.last_clicks_path)
    file_same = (file_before is not None and file_after is not None
                 and all((a is None and b is None)
                         or (a is not None and b is not None
                             and abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9)
                         for a, b in zip(file_before.pixels, file_after.pixels)))
    check("out-of-bounds typed pixel leaves node and file unchanged",
          refused is not None and refused.get("status") == "refused"
          and node_same and file_same,
          f"result={refused} pixels={(file_after.pixels if file_after else None)}")
    note = widget.cameraPixelNote.text
    try:
        kept = (float(green_edits[0].text.strip()),
                float(green_edits[1].text.strip()))
    except ValueError:
        kept = None
    check("out-of-bounds note names the range, old value stays",
          "outside" in note and kept is not None
          and abs(kept[0] - 196.5) < 1e-9 and abs(kept[1] - 129.0) < 1e-9,
          f"note={note!r} fields={kept}")

    for widget_name in ("Red", "Green", "Yellow"):
        u_edit, v_edit = widget.cameraPixelEdits[widget_name]
        u_edit.setText("150.0")
        v_edit.setText("150.0")
        widget.on_camera_pixel_edited(widget_name)
    check("identical pixels raise the identical warning",
          "identical" in widget.cameraPixelNote.text,
          f"note={widget.cameraPixelNote.text!r}")

    green_edits[0].setText("196.5")
    green_edits[1].setText("129.0")
    widget.on_camera_pixel_edited("Green")
    check("a distinct pixel clears the identical warning",
          "identical" not in widget.cameraPixelNote.text,
          f"note={widget.cameraPixelNote.text!r}")

    cleared_path = logic.clear_camera_clicks()
    if cleared_path is None:
        cleared_path = logic.last_clicks_path
    if cleared_path is not None:
        cleared, cleared_note = read_clicks(cleared_path)
    else:
        cleared, cleared_note = None, "nothing published"
    check("clear publishes the all-null shape",
          cleared is not None and all(pixel is None for pixel in cleared.pixels),
          cleared_note if cleared is None
          else f"pixels={cleared.pixels}")
    check("clear removes the marks",
          all(node is None or node.GetNumberOfControlPoints() == 0
              for node in _click_nodes()),
          f"points={_click_points()}")

    legacy = slicer.mrmlScene.AddNewNodeByClass(
        "vtkMRMLMarkupsFiducialNode", CAMERA_CLICKS_NODE_NAME)
    legacy.AddControlPoint(1.0, 2.0, 3.0)
    logic.set_camera_click("Red", *red_xy)
    check("a mark removes the pre-split legacy node, never reuses it",
          slicer.mrmlScene.GetFirstNodeByName(CAMERA_CLICKS_NODE_NAME) is None
          and _click_points() == [1, 0, 0],
          f"points={_click_points()}")
    logic.clear_camera_clicks()
    check("clear after the legacy mark empties all three nodes",
          all(node is None or node.GetNumberOfControlPoints() == 0
              for node in _click_nodes()),
          f"points={_click_points()}")

    # --- Spawn seeds the camera clicks: one typed rig-mm point, one publish ---
    import json as _json

    import numpy as _np

    from TargetingLib.logic import CAMERA_VIEWS as _CAMERA_VIEWS

    rig = _json.loads((folder / "camera-rig.json").read_text(encoding="utf-8"))

    def _file_placement(point):
        """Per-view pixel-or-None from the FILE only -- mirrors spawn_point."""
        placed = []
        for entry in rig["cameras"]:
            matrix = _np.asarray(entry["projection"], float)
            weighed = matrix @ _np.array([point[0], point[1], point[2], 1.0])
            w = float(weighed[2])
            if w <= 0.0:
                placed.append(None)
                continue
            u, v = float(weighed[0] / w), float(weighed[1] / w)
            if 0.0 <= u < int(entry["width"]) and 0.0 <= v < int(entry["height"]):
                placed.append((u, v))
            else:
                placed.append(None)
        return placed

    check("spawn fields default to the rig origin (the printed marker)",
          widget.spawnXEdit.text == "0.0" and widget.spawnYEdit.text == "0.0"
          and widget.spawnZEdit.text == "0.0",
          f"x={widget.spawnXEdit.text!r} y={widget.spawnYEdit.text!r} "
          f"z={widget.spawnZEdit.text!r}")
    widget.spawnXEdit.setText("0.0")
    widget.spawnYEdit.setText("0.0")
    widget.spawnZEdit.setText("0.0")
    spawned = widget.on_spawn()
    expected_origin = _file_placement((0.0, 0.0, 0.0))
    check("spawn seeds all three clicks at the rig origin",
          spawned is not None and spawned.get("status") == "spawned"
          and spawned.get("placed") == 3
          and all(pixel is not None for pixel in expected_origin),
          f"result={spawned} file-want={expected_origin}")

    seed_nodes = _click_nodes()
    check("spawn leaves one seeded mark in each click node",
          all(node is not None and node.GetNumberOfControlPoints() == 1
              for node in seed_nodes),
          f"points={_click_points()}")

    for _index, _widget_name in enumerate(_CAMERA_VIEWS):
        _u, _v = expected_origin[_index]
        _tag = ("c1", "c2", "c3")[_index]
        _want_ras = logic.pixel_to_camera_ras(_widget_name, _u, _v)
        _pos = [0.0, 0.0, 0.0]
        seed_nodes[_index].GetNthControlPointPosition(0, _pos)
        _ras_ok = max(abs(_pos[k] - _want_ras[k]) for k in range(3)) < 1e-6
        _label = seed_nodes[_index].GetNthControlPointLabel(0)
        _pixel_string = f"({_u:.0f}, {_v:.0f})"
        check(f"seeded {_tag} mark matches its file pixel",
              bool(_ras_ok) and _label.startswith(_tag)
              and _pixel_string in _label,
              f"mark={tuple(round(p, 3) for p in _pos)} "
              f"matrix={tuple(round(p, 3) for p in _want_ras)} "
              f"label={_label!r} want={_pixel_string!r}")
        print(f"SPAWN {_tag} pixel=({_u:.1f}, {_v:.1f}) "
              f"ras=({_want_ras[0]:.3f}, {_want_ras[1]:.3f}, {_want_ras[2]:.3f})")

    seeded_file, seeded_note = read_clicks(logic.last_clicks_path)
    _file_ok = seeded_file is not None
    _file_detail = seeded_note if seeded_file is None else f"pixels={seeded_file.pixels}"
    if seeded_file is not None:
        for _index in range(3):
            _got, _want = seeded_file.pixels[_index], expected_origin[_index]
            if (_got is None or _want is None
                    or abs(_got[0] - _want[0]) > 1e-6
                    or abs(_got[1] - _want[1]) > 1e-6):
                _file_ok = False
                _file_detail = (f"pixels={seeded_file.pixels} "
                                f"want={expected_origin}")
                break
    check("spawn publishes the three seeded pixels", _file_ok, _file_detail)

    rows_ok, rows_detail = seeded_file is not None, ""
    if seeded_file is not None:
        for index, widget_name in enumerate(("Red", "Green", "Yellow")):
            u_edit, v_edit = widget.cameraPixelEdits[widget_name]
            published = seeded_file.pixels[index]
            try:
                shown = (float(u_edit.text.strip()), float(v_edit.text.strip()))
            except ValueError:
                rows_ok, rows_detail = False, f"{widget_name} fields not numeric"
                break
            if (published is None or abs(shown[0] - published[0]) > 0.11
                    or abs(shown[1] - published[1]) > 0.11):
                rows_ok, rows_detail = False, (
                    f"{widget_name} shows {shown}, published {published}")
                break
            rows_detail = (f"c1={seeded_file.pixels[0]} "
                           f"c2={seeded_file.pixels[1]} c3={seeded_file.pixels[2]}")
    check("seeded pixel rows show the published pixels", rows_ok, rows_detail)

    check("spawned set reads clicked 3 of 3",
          "clicked 3 of 3" in logic.camera_status(),
          f"status={logic.camera_status()!r}")

    for _index, _widget_name in enumerate(_CAMERA_VIEWS):
        _tag = ("c1", "c2", "c3")[_index]
        _own = (slicer.app.layoutManager().sliceWidget(_widget_name)
                .mrmlSliceNode().GetID())
        _ids = _display_view_ids(seed_nodes[_index])
        check(f"seeded {_tag} shows only in its own view",
              _ids == [_own],
              f"views={_ids} own={_own}")
        print(f"SPAWN-VIEW {_tag} view={_own} list={_ids}")

    summary = widget.spawnNote.text
    check("spawn summary lists all three pixels",
          all(f"({_u:.1f}, {_v:.1f})" in summary
              for _u, _v in expected_origin),
          f"summary={summary!r}")

    _seed_pos = [0.0, 0.0, 0.0]
    seed_nodes[0].GetNthControlPointPosition(0, _seed_pos)
    _seed_moved = (float(_seed_pos[0]) + 10.0, float(_seed_pos[1]),
                   float(_seed_pos[2]))
    _seed_expected = logic.ras_to_camera_pixel("Red", _seed_moved)
    seed_nodes[0].SetNthControlPointPosition(0, *_seed_moved)
    _reseeded = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.1)
        current, _ = read_clicks(logic.last_clicks_path)
        if (current is not None and current.pixels[0] is not None
                and abs(current.pixels[0][0] - _seed_expected[0]) < 1.0
                and abs(current.pixels[0][1] - _seed_expected[1]) < 1.0):
            _reseeded = current
            break
    check("a seeded mark still republishes when edited", _reseeded is not None,
          f"expected=({_seed_expected[0]:.1f}, {_seed_expected[1]:.1f}) "
          f"got={(_reseeded.pixels[0] if _reseeded is not None else None)}")

    # Re-spawn over an already-seeded set: the marks move, nothing stacks.
    _before = []
    for _node in _click_nodes():
        _pos = [0.0, 0.0, 0.0]
        if _node.GetNumberOfControlPoints() == 1:
            _node.GetNthControlPointPosition(0, _pos)
        _before.append(tuple(_pos))
    widget.spawnXEdit.setText("0.0")
    widget.spawnYEdit.setText("0.0")
    widget.spawnZEdit.setText("0.0")
    again = widget.on_spawn()
    _after = []
    for _node in _click_nodes():
        _pos = [0.0, 0.0, 0.0]
        if _node.GetNumberOfControlPoints() == 1:
            _node.GetNthControlPointPosition(0, _pos)
        _after.append(tuple(_pos))
    _moved = any(max(abs(_after[i][k] - _before[i][k]) for k in range(3)) > 1e-6
                 for i in range(3))
    check("re-spawn over a seeded set moves, never stacks",
          again is not None and again.get("status") == "spawned"
          and all(node.GetNumberOfControlPoints() <= 1 for node in _click_nodes())
          and _moved,
          f"moved={_moved} before={_before} after={_after}")

    # Partial replace over a full set: a point only one camera can see must
    # clear exactly the other two slots (node, file pixel, row).
    _mixed_point, _mixed_want = None, None
    for _cand in ((-200.0, 0.0, 0.0), (200.0, 0.0, 0.0), (0.0, -200.0, 0.0),
                  (0.0, 200.0, 0.0), (0.0, 0.0, 200.0)):
        _pred = _file_placement(_cand)
        _seen = [p for p in _pred if p is not None]
        if 0 < len(_seen) < 3:
            _mixed_point, _mixed_want = _cand, _pred
            break
    mixed = None
    mixed_file = None
    if _mixed_point is None:
        check("partial spawn clears exactly the out-of-picture slots", False,
              "no mixed-visibility candidate point found for this rig")
    else:
        widget.spawnXEdit.setText(f"{_mixed_point[0]:g}")
        widget.spawnYEdit.setText(f"{_mixed_point[1]:g}")
        widget.spawnZEdit.setText(f"{_mixed_point[2]:g}")
        mixed = widget.on_spawn()
        mixed_file, _ = read_clicks(logic.last_clicks_path)
        mixed_nodes = _click_nodes()
        _ok = mixed is not None and mixed.get("status") == "spawned"
        _detail = []
        for _index in range(3):
            _want = _mixed_want[_index]
            _count = mixed_nodes[_index].GetNumberOfControlPoints()
            _got = (mixed_file.pixels[_index]
                    if mixed_file is not None else "no file")
            if _want is None:
                _ok = (_ok and _count == 0 and _got is None)
            else:
                _ok = (_ok and _count == 1 and _got is not None
                       and abs(_got[0] - _want[0]) < 1e-6
                       and abs(_got[1] - _want[1]) < 1e-6)
            _detail.append(f"c{_index + 1} want={_want} marks={_count} file={_got}")
        check("partial spawn clears exactly the out-of-picture slots", bool(_ok),
              f"point={_mixed_point} " + " ".join(_detail))
        _rows = {name: (widget.cameraPixelEdits[name][0].text.strip(),
                        widget.cameraPixelEdits[name][1].text.strip())
                 for name in ("Red", "Green", "Yellow")}
        _rows_ok = all((not u and not v) if _mixed_want[i] is None else (u and v)
                       for i, (u, v) in enumerate(_rows.values()))
        check("partial spawn clears exactly the matching pixel rows", bool(_rows_ok),
              f"rows={_rows}")

    # Garbage from a NON-EMPTY state: refused, nothing moves.
    _state_before = {i: (node.GetNumberOfControlPoints()
                         if node is not None else -1)
                     for i, node in enumerate(_click_nodes())}
    _pixels_before = mixed_file.pixels if mixed_file is not None else None
    widget.spawnXEdit.setText("abc")
    refused = widget.on_spawn()
    slicer.app.processEvents()
    refused_file, _ = read_clicks(logic.last_clicks_path)
    _state_after = {i: (node.GetNumberOfControlPoints()
                        if node is not None else -1)
                    for i, node in enumerate(_click_nodes())}
    check("garbage spawn is refused, a non-empty state stays untouched",
          refused is not None and refused.get("status") == "refused"
          and _state_after == _state_before
          and sum(v for v in _state_before.values() if v > 0) >= 1
          and refused_file is not None
          and refused_file.pixels == _pixels_before
          and bool(widget.spawnNote.text),
          f"result={refused} note={widget.spawnNote.text!r} "
          f"before={_state_before} after={_state_after}")

    widget.spawnXEdit.setText("1000.0")
    widget.spawnYEdit.setText("1000.0")
    widget.spawnZEdit.setText("1000.0")
    far = widget.on_spawn()
    far_nodes = _click_nodes()
    check("all-outside spawn leaves three empty click nodes",
          far is not None and far.get("status") == "spawned"
          and far.get("placed") == 0
          and all(node is not None and node.GetNumberOfControlPoints() == 0
                  for node in far_nodes),
          f"result={far} points={_click_points()}")
    far_file, far_note = read_clicks(logic.last_clicks_path)
    check("all-outside spawn publishes the all-null shape",
          far_file is not None
          and all(pixel is None for pixel in far_file.pixels),
          far_note if far_file is None else f"pixels={far_file.pixels}")
    far_rows = {name: (widget.cameraPixelEdits[name][0].text.strip(),
                       widget.cameraPixelEdits[name][1].text.strip())
                for name in ("Red", "Green", "Yellow")}
    check("all-outside spawn clears the pixel rows",
          all(not u and not v for u, v in far_rows.values()),
          f"rows={far_rows}")
    check("all-outside spawn notes outside the picture",
          "outside the picture" in widget.spawnNote.text,
          f"note={widget.spawnNote.text!r}")

    # The missing-view refusal in _camera_view_ids() is deliberately not smoke-
    # driven: Slicer keeps slice widgets alive (hidden) across layout changes,
    # so no reachable layout has a truly missing view for the guard to see.
    # The guard is an API contract fixed by review, not by layout tricks.

    failed = [name for name, ok in _CHECKS if not ok]
    if failed:
        print("SMOKE FAIL: " + ", ".join(failed))
    else:
        print(f"SMOKE PASS {len(_CHECKS)}/{len(_CHECKS)}")
    slicer.app.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("SMOKE FAIL: exception")
        sys.exit(1)
