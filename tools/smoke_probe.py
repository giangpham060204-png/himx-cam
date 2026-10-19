"""In-Slicer smoke test for the probe the camera sandbox follows.

Runs in a separate Slicer:

    timeout 300 <slicer> --no-splash --python-script tools/smoke_probe.py

Moves a probe the way an operator would -- a click in a slice view, then a drag
-- and checks what the logic published: a file the sandbox can read, replaced
atomically, with the same point the scene now shows, and with nothing written to
the target log. Prints one line per check and finally "SMOKE PASS n/n" or
"SMOKE FAIL: names".
"""

import pathlib
import sys
import tempfile
import time
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

_CHECKS = []


def check(name, ok, detail=""):
    _CHECKS.append((name, bool(ok)))
    print(f"CHECK {name}: {'ok' if ok else 'FAIL'} {detail}")


def main():
    import slicer

    module_dir = PROJECT_ROOT / "SlicerTargeting" / "Targeting"
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))
    import TargetingLib.logic as logic_module

    from targeting.probe import PROBE_FILE, read_probe

    folder = pathlib.Path(tempfile.mkdtemp(prefix="probe-"))
    logic_module.LOGS_DIR = folder          # nothing lands in the real logs/
    logic = logic_module.TargetingLogic()

    node = logic.probe_node()
    check("the probe node exists", node is not None and node.GetName() == "SandboxProbe")

    # The view centre is where the field is: a hardcoded pixel is a guess that
    # this smoke test proved wrong once already (R = -93 mm, outside the field).
    slice_widget = slicer.app.layoutManager().sliceWidget("Red")
    centre = slice_widget.sliceView().size
    cx, cy = centre.width() / 2.0, centre.height() / 2.0
    result = logic.set_probe_from_click("XY", cx, cy)
    check("a click moves the probe", result["status"] == "probed",
          f"cube={tuple(round(v, 1) for v in result['cube'])}")

    probe, note = read_probe(folder / PROBE_FILE)
    check("the point is published where the sandbox reads it", probe is not None, note)
    check("the published point is the one the click made",
          probe is not None and all(abs(a - b) < 1e-6
                                    for a, b in zip(probe.cube, result["cube"])),
          f"published={None if probe is None else probe.cube}")

    check("nothing was placed as a target",
          logic.placed == 0 and logic.last_log_path is None,
          f"placed={logic.placed}")
    check("nothing was written to the target log",
          list(folder.glob("targets-*.csv")) == [])

    first = probe.cube if probe is not None else None
    logic.set_probe_from_click("XY", cx + 20.0, cy + 10.0)
    again = read_probe(folder / PROBE_FILE)[0]
    check("a second click moves it, and the file follows",
          first is not None and again is not None and again.cube != first,
          f"{None if first is None else tuple(round(v, 1) for v in first)} -> "
          f"{None if again is None else tuple(round(v, 1) for v in again.cube)}")
    check("no temporary file is left behind",
          list(folder.glob("*.tmp")) == [], str(list(folder.glob("*.tmp"))))

    timer = logic.watch_probe()
    check("following installs a single-shot timer",
          timer.isSingleShot() and timer.interval == 150,
          f"interval={timer.interval}")

    before_drag = read_probe(folder / PROBE_FILE)[0]
    node = logic.probe_node()
    node.SetNthControlPointPosition(0, before_drag.ras[0], before_drag.ras[1],
                                    before_drag.ras[2] + 12.0)
    deadline = time.time() + 3.0
    after_drag = before_drag
    while time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.02)
        after_drag = read_probe(folder / PROBE_FILE)[0]
        if after_drag is not None and abs(after_drag.ras[2] - (before_drag.ras[2] + 12.0)) < 1e-6:
            break
    check("a dragged probe is published too",
          after_drag is not None
          and abs(after_drag.ras[2] - (before_drag.ras[2] + 12.0)) < 1e-6,
          f"ras z {before_drag.ras[2]:.1f} -> "
          f"{None if after_drag is None else round(after_drag.ras[2], 1)}")

    logic.stop_watch()
    check("a click outside the field is refused by name",
          _outside_is_refused(logic))

    failed = [name for name, ok in _CHECKS if not ok]
    if failed:
        print("SMOKE FAIL: " + ", ".join(failed))
    else:
        print(f"SMOKE PASS {len(_CHECKS)}/{len(_CHECKS)}")
    slicer.app.quit()


def _outside_is_refused(logic):
    """A click far outside the field must return 'rejected', not a probe."""
    from targeting.converter import OutOfFieldError

    try:
        logic.set_probe_from_click("XY", 5.0, 5.0)
    except OutOfFieldError:
        return True
    except Exception:
        traceback.print_exc()
        return False
    return False


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("SMOKE FAIL: exception")
        sys.exit(1)
