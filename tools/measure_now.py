"""Run print_slicer_constants.py with guaranteed-fresh code. RUN THIS, not the module.

Legacy utility: normal operation reads the pixel->RAS mapping live via
slicer_bridge/live_frame.py; this remains for inspecting the raw matrices.

Usage, from Slicer's Python Console:

    exec(open('<path-to-himx-cam>/tools/measure_now.py').read())

Why `exec(open(...).read())` instead of `import`
------------------------------------------------
Slicer caches imported modules in sys.modules for the whole session, so
`import print_slicer_constants` can return a stale object from memory after
the file on disk changed, and a stale module may not even have a reload
method. exec() has no cache: it reads the file and runs it, every time. The
only outside name it needs is `slicer`, which the console already provides.

Keep this file tiny and free of logic. All measurement lives in
print_slicer_constants.py; this only guarantees fresh code is used.
"""

import pathlib
import sys

# No hardcoded fallback: panic instead of guessing (see make_field_cube).


def _resolve_project_root(_globals=None, _env=None):
    """Project root for TOOLS_DIR without scanning the disk.

    Same precedence as make_field_cube: PROJECT_ROOT_OVERRIDE global >
    HIMX_CAM_ROOT env > truthful __file__ marker walk, else panic.
    Returns the checkout root; TOOLS_DIR is derived from it.
    """
    import os
    import pathlib

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


_PROJECT_ROOT = _resolve_project_root()
TOOLS_DIR = str(_PROJECT_ROOT / "tools")
TARGET = "print_slicer_constants"


def _clear_caches():
    """Drop the cached module and its bytecode, tolerating partial failures."""
    import os

    path = os.path.join(TOOLS_DIR, TARGET + ".py")
    for directory in (TOOLS_DIR, os.path.join(TOOLS_DIR, "__pycache__")):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if name.startswith(TARGET) and name.endswith(".pyc"):
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass
    sys.modules.pop(TARGET, None)


def run():
    """Re-import the module from disk and return it."""
    if TOOLS_DIR not in sys.path:
        sys.path.insert(0, TOOLS_DIR)
    _clear_caches()
    import importlib

    return importlib.import_module(TARGET)


def report():
    """Print the measured constants. Safe to call from anywhere."""
    import json

    module = run()
    print(module.module_report())
    print(json.dumps(module.measure(), indent=2))
    print("Merge pixel_size_mm and view_axis_map into frame.json.")


# Only auto-run when exec'd from a console, not when imported by a test.
if __name__ in ("__main__", "__builtin__", "builtins"):
    try:
        report()
    except Exception as exc:  # noqa: BLE001 - this is a console bootstrap
        print(f"{type(exc).__name__}: {exc}")