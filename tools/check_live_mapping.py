"""Verify the live touch chain against Slicer's own Data Probe.

Run in Slicer's Python Console:

    exec(open('/home/gianghp/projects/himx-cam/tools/check_live_mapping.py').read())

Part 1 prints the mapping read live from Slicer's own view matrices, next to
the stored reference in frame.json (the live values always win).

Part 2 walks you through three spots, one per view, in the order Red, Green,
Yellow. For each spot it says which view to use; you move the mouse inside
the cube there and hold it still. A spot only counts once the mouse and
Slicer's own crosshair have both stopped moving for two consecutive checks.
Comparing positions while the mouse is still moving would give a few pixels
of phantom error: the cursor and the crosshair are read at slightly different
instants then, and the gap widens with speed. Each still spot is converted
with the same code the targeting module uses and compared with Slicer's own
reading of that pixel (the Data Probe value, taken from the crosshair).
Nothing needs to be measured or typed by hand.

PASS means the derived mapping agrees with Slicer's hit-testing to within
about a display pixel (2.5mm at the default zoom). Half-pixel coordinate
conventions are left visible in the printed cursor offset rather than hidden.
After changing screen or resizing, you can simply run it again. The script
also reloads the project's modules from disk on every run, so it always
tests the current code even inside a Slicer session that ran it before.
"""

from __future__ import annotations

import pathlib
import sys

# Hardcoded on purpose. This file is exec()'d in Slicer's Python Console,
# where __file__ is not the path of this script, so deriving the project root
# from it silently lands somewhere else and the imports fail with
# "No module named 'targeting'". measure_now.py hardcodes for the same reason.
PROJECT_ROOT = pathlib.Path("/home/gianghp/projects/himx-cam")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from targeting.converter import OutOfFieldError, convert  # noqa: E402
from targeting.frame import Frame  # noqa: E402

FRAME_PATH = PROJECT_ROOT / "frame.json"

PASS_TOLERANCE_MM = 2.5  # one display pixel is 1.825mm; the rest covers rounding
PHASE_ORDER = ("XY", "XZ", "YZ")  # guided order: Red, then Green, then Yellow

_RAS_AXES = ("R", "A", "S")


def cube_from_ras(ras, frame):
    """RAS to Cube: Cube counts from the field's lower corner."""
    half = {axis: frame.field_size_mm[axis] / 2.0 for axis in _RAS_AXES}
    return tuple(ras[i] + half[axis] for i, axis in enumerate(_RAS_AXES))


def slicer_pixel_from_xyz(xyz, qt_height_px, display_scale):
    """The Qt-space pixel Slicer itself ties to a crosshair XYZ sample.

    XYZ is in matrix pixels with a bottom-left origin; Qt pixels start at the
    top-left, and Qt rows count 0..H-1, so the row of a point is (H - 1 - y).
    """
    return (xyz[0] / display_scale, qt_height_px - (xyz[1] + 1.0) / display_scale)


def compare_sample(sample, frame):
    """Convert one hovered pixel and compare with Slicer's own reading.

    Returns (per-axis errors in mm, worst absolute error).
    """
    ours = convert(
        sample["view"], sample["u_px"], sample["v_px"], sample["depth_mm"], frame
    )
    truth = cube_from_ras(sample["slicer_ras"], frame)
    errors = [ours[i] - truth[i] for i in range(3)]
    return errors, max(abs(e) for e in errors)


def verdict(max_error_mm, tolerance_mm=PASS_TOLERANCE_MM):
    return "PASS" if max_error_mm <= tolerance_mm else "FAIL"


def _print_live_mapping(bridge, base, frame, layout):
    from targeting.view_mapping import constants_for_view  # noqa: PLC0415

    print("live mapping read from Slicer's own view matrices:")
    for view in ("XY", "XZ", "YZ"):
        constants = constants_for_view(bridge.read_view_data(layout=layout)[view])
        stored = base.view_axis_map[view]
        stored_map = {
            name: [stored_axis_sign[0], stored_axis_sign[1]]
            for name, stored_axis_sign in stored.items()
        }
        live = {name: list(value) for name, value in constants.axis_map.items()}
        agreement = "ok" if live == stored_map else "DIFFERS from stored reference"
        print(
            f"  {view} ({bridge.VIEW_WIDGETS[view]:6s})  "
            f"{constants.mm_per_px:.4f} mm/px  "
            f"centre ({constants.center_px[0]:.1f}, {constants.center_px[1]:.1f})  "
            f"u {tuple(live['u'])} v {tuple(live['v'])} depth {tuple(live['depth'])}  "
            f"[{agreement}]"
        )


def _refresh_project_imports():
    """Reload the project's modules from disk before use.

    Slicer's Python keeps imported modules in sys.modules for the whole
    session, so without reloading a rerun would test the previously imported
    code instead of what is on disk.

    Reloading rebinds class objects, which must not happen under pytest, so
    this is a no-op outside a Slicer session.
    """
    import importlib  # noqa: PLC0415
    import sys  # noqa: PLC0415

    if "slicer" not in sys.modules:
        return

    from targeting import converter, frame, view_mapping  # noqa: PLC0415

    importlib.reload(frame)
    importlib.reload(converter)
    importlib.reload(view_mapping)

    import slicer_bridge.live_frame as live_frame  # noqa: PLC0415

    importlib.reload(live_frame)

    # exec() runs this file in the console namespace; rebind the names that
    # its functions resolve at call time so they see the reloaded code too.
    names = globals()
    names["convert"] = converter.convert
    names["OutOfFieldError"] = converter.OutOfFieldError
    names["Frame"] = frame.Frame
    names["constants_for_view"] = view_mapping.constants_for_view
    names["frame_from_views"] = view_mapping.frame_from_views


def run():
    _refresh_project_imports()

    import qt  # noqa: PLC0415
    import slicer  # noqa: PLC0415

    import slicer_bridge.live_frame as bridge  # noqa: PLC0415
    from targeting.view_mapping import (  # noqa: PLC0415
        constants_for_view,
        frame_from_views,
    )

    base = Frame.load(FRAME_PATH)
    layout = slicer.app.layoutManager()

    _print_live_mapping(bridge, base, frame_from_views(base, bridge.read_view_data(layout=layout)), layout)

    crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
    if crosshair is None:
        print("no crosshair node in the scene; cannot capture samples")
        return

    phases = [(view, bridge.VIEW_WIDGETS[view]) for view in PHASE_ORDER]

    print()
    print("three spots, one per view. At each spot, move the mouse inside the")
    print("little cube in the named view, then hold still for about half a")
    print("second; a spot is accepted once both the mouse and Slicer settle.")

    # Running the script twice in one session must not leave two timers alive.
    previous = globals().get("_CHECK_LIVE_TIMER")
    if previous is not None:
        try:
            previous.stop()
        except Exception:  # noqa: BLE001
            pass

    state = {
        "samples": [],
        "timer": None,
        "prev_cursor": None,
        "prev_key": None,
        "last_skip": None,
        "phase": 0,
        "hinted": set(),
        "done": False,
        "on_tick": None,
    }

    def read_crosshair(caller):
        xyz = [0.0, 0.0, 0.0]
        node = caller.GetCursorPositionXYZ(xyz)
        ras = [0.0, 0.0, 0.0]
        if node is None or not caller.GetCursorPositionRAS(ras):
            return None
        view = bridge.WIDGET_VIEWS.get(node.GetName())
        if view is None:
            return None
        return node, view, [float(c) for c in xyz], [float(c) for c in ras]

    def finish():
        state["done"] = True
        if state["timer"] is not None:
            state["timer"].stop()
        _print_summary(state["samples"])

    def announce():
        _view, name = phases[state["phase"]]
        print()
        print(
            f"=== spot {state['phase'] + 1} of {len(phases)}: "
            f"hover inside the cube in the {name.upper()} view and hold still ==="
        )

    def on_tick():
        if state["done"]:
            return

        cursor = qt.QCursor.pos()
        now_px = (cursor.x(), cursor.y())
        found = read_crosshair(crosshair)
        key = (
            None
            if found is None
            else (found[0].GetName(), round(found[2][0], 2), round(found[2][1], 2))
        )

        # A spot counts only when both readings have been identical for two
        # consecutive ticks. Reading them mid-motion compares two different
        # instants and invents a few pixels of error.
        still = (
            state["prev_cursor"] is not None
            and abs(now_px[0] - state["prev_cursor"][0]) <= 2
            and abs(now_px[1] - state["prev_cursor"][1]) <= 2
        )
        stable = key is not None and key == state["prev_key"]
        state["prev_cursor"] = now_px
        state["prev_key"] = key
        if not (still and stable):
            return

        node, view, xyz, ras = found
        wanted_view, wanted_name = phases[state["phase"]]
        if view != wanted_view:
            token = (state["phase"], view)
            if token not in state["hinted"]:
                state["hinted"].add(token)
                print(
                    f"  (that spot is in the {bridge.VIEW_WIDGETS[view]} view; "
                    f"this one needs the {wanted_name} view)"
                )
            return
        widget = layout.sliceWidget(node.GetName())
        slice_view = widget.sliceView()
        local = slice_view.mapFromGlobal(cursor)
        u_px, v_px = float(local.x()), float(local.y())

        view_data = bridge.read_view_data(layout=layout)
        frame = frame_from_views(base, view_data)
        depth_mm = float(widget.sliceLogic().GetSliceNode().GetSliceOffset())
        sample = {
            "view": view,
            "u_px": u_px,
            "v_px": v_px,
            "depth_mm": depth_mm,
            "slicer_ras": ras,
        }
        try:
            errors, worst = compare_sample(sample, frame)
        except OutOfFieldError as exc:
            note = f"outside the field ({exc})"
            if note != state["last_skip"]:
                print(f"  skipped a still spot in {view}: {note}")
                print("  move nearer the middle of the cube and hold still again")
                state["last_skip"] = note
            return
        state["last_skip"] = None

        constants = constants_for_view(view_data[view])
        slicer_px = slicer_pixel_from_xyz(
            xyz, view_data[view]["qt_height_px"], constants.display_scale
        )
        delta_x = u_px - slicer_px[0]
        delta_y = v_px - slicer_px[1]

        sample["errors_mm"] = [round(e, 3) for e in errors]
        sample["worst_mm"] = round(worst, 3)
        sample["cursor_delta_px"] = [round(delta_x, 2), round(delta_y, 2)]
        state["samples"].append(sample)
        state["prev_key"] = None  # the next spot must settle from scratch

        print(
            f"  spot {state['phase'] + 1} done: {wanted_name} view, "
            f"pixel ({u_px:.0f}, {v_px:.0f}), depth {depth_mm:.1f}mm -> "
            f"error {worst:.3f}mm  {verdict(worst)}   "
            f"(cursor vs Slicer: {delta_x:+.2f}, {delta_y:+.2f} px)"
        )
        state["phase"] += 1
        if state["phase"] >= len(phases):
            finish()
        else:
            announce()

    state["on_tick"] = on_tick
    announce()
    timer = qt.QTimer()
    timer.setInterval(250)
    timer.timeout.connect(on_tick)
    timer.start()
    state["timer"] = timer
    globals()["_CHECK_LIVE_TIMER"] = timer


def _print_summary(samples):
    print()
    print("summary (errors are ours minus Slicer, in mm):")
    worst = 0.0
    for i, sample in enumerate(samples, start=1):
        worst = max(worst, sample["worst_mm"])
        delta = sample.get("cursor_delta_px")
        delta_text = (
            f"  cursor offset ({delta[0]:+.2f}, {delta[1]:+.2f}) px" if delta else ""
        )
        print(
            f"  {i}: {sample['view']}  "
            f"pixel ({sample['u_px']:.0f}, {sample['v_px']:.0f})  "
            f"depth {sample['depth_mm']:.1f}  "
            f"errors (R {sample['errors_mm'][0]:+.3f}, "
            f"A {sample['errors_mm'][1]:+.3f}, "
            f"S {sample['errors_mm'][2]:+.3f}){delta_text}"
        )
    print(f"worst error: {worst:.3f}mm   tolerance: {PASS_TOLERANCE_MM}mm")
    if worst <= PASS_TOLERANCE_MM:
        print("PASS - the touch chain matches Slicer's own hit testing.")
    else:
        print(
            "FAIL - the cursor offset column tells the story: a constant "
            "sub-pixel offset is harmless rounding, anything bigger means a "
            "real disagreement. Rerun once (hold still a moment longer); if it "
            "fails again, capture this output and report it."
        )


if __name__ in ("__main__", "__builtin__", "builtins"):
    # Slicer's console exec()s this file with __name__ == "__main__".
    # Under pytest (or any import) it must stay dormant: importing a module
    # must never run Slicer code or reload other modules.
    run()

