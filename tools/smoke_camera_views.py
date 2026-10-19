"""Slicer smoke: three pictures land in three views, and nothing else changes.

    uv run tools/camera_sandbox.py --write-frames /tmp/opencode/frames
    timeout 300 <slicer> --no-splash --python-script tools/smoke_camera_views.py
"""

import os
import pathlib
import sys
import tempfile
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
    from TargetingLib.logic import CAMERA_VIEWS, TargetingLogic
    from targeting.clicks import read_clicks

    folder = pathlib.Path(os.environ["HIMX_TEST_FRAMES"])
    logic = TargetingLogic()

    result = logic.load_camera_views(folder)
    check("three views loaded", result["status"] == "loaded" and len(result["views"]) == 3,
          str(result.get("problems", ())))
    for widget_name, node_name in result["views"].items():
        widget = slicer.app.layoutManager().sliceWidget(widget_name)
        background = widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID()
        node = slicer.mrmlScene.GetNodeByID(background)
        check(f"{widget_name} shows {node_name}",
              node is not None and node.GetName() == node_name)
        check(f"{widget_name} is axial",
              abs(widget.mrmlSliceNode().GetSliceToRAS().GetElement(0, 2)) < 1e-9)
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
    logic.set_camera_click("Red", *red_xy)
    logic.set_camera_click("Green", *green_xy)
    check("two clicks publish nothing", logic.publish_clicks() is None)
    logic.set_camera_click("Yellow", *yellow_xy)
    path = logic.publish_clicks(pathlib.Path(tempfile.mkdtemp()))
    clicks, note = read_clicks(path)
    check("three clicks publish one readable set", clicks is not None, note)
    expected = (("Red", red_xy), ("Green", green_xy), ("Yellow", yellow_xy))
    for index, (widget_name, xy) in enumerate(expected):
        u, v = logic.camera_pixel(widget_name, *xy)
        if clicks is None:
            check(f"pixel {index} is {widget_name}'s click", False, "no clicks published")
        else:
            got = clicks.pixels[index]
            check(f"pixel {index} is {widget_name}'s click",
                  abs(got[0] - u) < 6.0 and abs(got[1] - v) < 6.0,
                  f"published=({got[0]:.1f}, {got[1]:.1f}) "
                  f"{widget_name}=({u:.1f}, {v:.1f})")
    check("outside clicks are refused by view name", _outside_is_refused(logic))

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
