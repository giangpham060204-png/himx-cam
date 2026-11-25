"""Console entry point for the marker plate: place it, check it, no panel.

In Slicer's Python Console:

    exec(open('<path-to-himx-cam>/tools/load_markers.py').read())

Without Slicer, to validate the file only:

    python tools/load_markers.py --check

The work lives in two places, so the panel and this script cannot disagree:

* ``targeting/marker_plate.py``  -- the numbers, validation, the map (no Slicer)
* ``TargetingLib/marker_plate.py`` -- the scene nodes and the camera check

The panel in the Targeting module is the same code, so this file is only a
bootstrap for people who prefer the console.
"""

from __future__ import annotations

import pathlib
import sys

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
        "to the directory containing frame.json, or set PROJECT_ROOT_OVERRIDE "
        f"in the Slicer console before exec(). __file__={here!r}"
    )


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "SlicerTargeting" / "Targeting") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "SlicerTargeting" / "Targeting"))


def check_config():
    """Validate the plate file without Slicer, and print what it describes."""
    from targeting.marker_plate import load_plate

    plate = load_plate()
    print(f"{plate['path']}: {plate['width']:.1f} x {plate['height']:.1f} mm "
          f"plate, {len(plate['markers'])} markers, cube origin "
          f"({plate['origin'][0]:.1f}, {plate['origin'][1]:.1f}, "
          f"{plate['origin'][2]:.1f}) mm")
    for marker in plate["markers"]:
        print(f"  {marker['label']:>6s}  ({marker['mm'][0]:7.2f}, "
              f"{marker['mm'][1]:7.2f}, {marker['z']:5.1f}) mm")
    if not plate["coplanar"]:
        print(f"z spans {plate['z_spread']:.2f} mm: the markers are not one "
              f"plane, so the pixel map will be refused")
    print("order matters: MarkerClicks points are matched to this list by index")
    if len(plate["markers"]) == 4:
        print("four markers fit exactly: add a fifth to get a check that can "
              "fail")
    return plate


def run():
    from TargetingLib import marker_plate

    return marker_plate.run()


if __name__ == "__main__" and "--check" in sys.argv:
    try:
        check_config()
    except Exception as exc:  # noqa: BLE001 - one line out, one line back
        print(f"{type(exc).__name__}: {exc}")
        sys.exit(1)
elif __name__ in ("__main__", "__builtin__", "builtins"):
    run()
elif "slicer" in sys.modules:
    # exec() in the Slicer console runs this file in the console's globals.
    try:
        run()
    except Exception as exc:  # noqa: BLE001 - console bootstrap
        print(f"{type(exc).__name__}: {exc}")
