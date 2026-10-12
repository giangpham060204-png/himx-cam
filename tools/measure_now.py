"""Run print_slicer_constants.py with guaranteed-fresh code. RUN THIS, not the module.

Legacy utility: normal operation reads the pixel->RAS mapping live via
slicer_bridge/live_frame.py; this remains for inspecting the raw matrices.

Usage, from Slicer's Python Console:

    exec(open('/home/gianghp/projects/himx-cam/tools/measure_now.py').read())

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

import sys

TOOLS_DIR = "/home/gianghp/projects/himx-cam/tools"
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