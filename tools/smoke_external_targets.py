"""In-Slicer smoke test for camera-derived targets.

Runs in a separate Slicer:

    timeout 300 <slicer> --no-splash --python-script tools/smoke_external_targets.py

Writes a throwaway log holding three good rows, one malformed row and one row
outside the field, loads the module directly, presses the button's handler, and
checks what appeared. Prints one line per check and finally "SMOKE PASS n/n" or
"SMOKE FAIL: names".
"""

import pathlib
import sys
import tempfile
import traceback
from datetime import datetime

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


def _write_logs(folder):
    """Three good rows, one malformed row, one row outside the field."""
    from targeting.camera_log import HEADER, format_camera_row

    when = datetime(2026, 10, 19, 9, 0, 0)
    pixels = ((160.0, 160.0), (158.0, 161.0), (161.0, 158.0))
    good = [format_camera_row(cube=(50.0 + k, 50.0, 50.0),
                              ras=(k, 0.0, 0.0),
                              residual_px=0.2, pixels=pixels, when=when)
            for k in (-2.0, 0.0, 2.0)]
    outside = format_camera_row(cube=(50.0, 50.0, 50.0), ras=(900.0, 0.0, 0.0),
                                residual_px=0.2, pixels=pixels, when=when)
    noisy = format_camera_row(cube=(50.0, 50.0, 50.0), ras=(0.0, 0.0, 0.0),
                              residual_px=18.8, pixels=pixels, when=when)
    path = folder / "camera-targets-2026-10-19.csv"
    path.write_text(",".join(HEADER) + "\n"
                    + "\n".join(good + ["broken", outside, noisy]) + "\n")
    return folder


def main():
    import slicer

    module_dir = PROJECT_ROOT / "SlicerTargeting" / "Targeting"
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))
    from TargetingLib.logic import TargetingLogic

    logic = TargetingLogic()
    folder = _write_logs(pathlib.Path(tempfile.mkdtemp(prefix="camera-targets-")))

    result = logic.load_external_targets(folder)
    check("three good rows load", result["loaded"] == 3, f"loaded={result['loaded']}")
    check("three bad rows are reported", result["skipped"] == 3, f"skipped={result['skipped']}")
    check("the malformed row is named",
          any("line" in problem for problem in result["problems"]),
          str(result["problems"][:1]))
    check("the high-residual row is named",
          any("residual" in problem for problem in result["problems"]),
          str([problem for problem in result["problems"] if "residual" in problem][:1]))

    node = slicer.mrmlScene.GetFirstNodeByName("ExternalTargets")
    check("the node exists", node is not None)
    check("the node holds three points", node is not None and node.GetNumberOfControlPoints() == 3)
    check("the points are labelled C1..C3",
          node is not None and [node.GetNthControlPointLabel(i) for i in range(3)] == ["C1", "C2", "C3"])

    again = logic.load_external_targets(folder)
    node = slicer.mrmlScene.GetFirstNodeByName("ExternalTargets")
    check("pressing twice does not duplicate",
          again["loaded"] == 3 and node.GetNumberOfControlPoints() == 3,
          f"points={node.GetNumberOfControlPoints()}")

    empty = logic.load_external_targets(pathlib.Path(tempfile.mkdtemp()))
    check("an empty folder is not an error", empty["status"] == "nothing")

    failed = [name for name, ok in _CHECKS if not ok]
    if failed:
        print("SMOKE FAIL: " + ", ".join(failed))
    else:
        print(f"SMOKE PASS {len(_CHECKS)}/{len(_CHECKS)}")
    pathlib.Path("/tmp/opencode").mkdir(parents=True, exist_ok=True)
    slicer.util.mainWindow().grab().save("/tmp/opencode/task10-slicer.png")
    slicer.app.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("SMOKE FAIL: exception")
        sys.exit(1)
