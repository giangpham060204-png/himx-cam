"""Check the converter against Slicer's own reference positions.

Run on a plain terminal, no Slicer needed:

    python3 tools/verify_converter.py

Reads reference positions from stdin as the JSON that make_refpoints.py
prints, or from a saved file. Pass criterion: max error < 0.01 mm.

This checks the RAS <-> Cube relationship and the depth axis. Pixel accuracy
is no longer checkable here (and no longer needs stored constants): it is
verified live inside Slicer by tools/check_live_mapping.py.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.converter import OutOfFieldError, convert  # noqa: E402
from targeting.field_fit import mm_per_pixel_for_fit, view_center_px  # noqa: E402
from targeting.frame import Frame  # noqa: E402

FRAME_PATH = pathlib.Path(__file__).resolve().parents[1] / "frame.json"
PASS_MM = 0.01


def cube_from_ras(ras, half):
    """RAS to Cube: Cube counts from the field corner, RAS from the centre."""
    return tuple(r + h for r, h in zip(ras, half))


def load_frame(viewport_px=(693.0, 137.0)):
    """Load frame.json and fill the pixel fields the depth sanity call needs.

    Screen-dependent values are not stored any more: a resize or a different
    monitor used to invalidate them silently. This tool compares RAS values
    only, so a nominal viewport is enough; live pixel behaviour is checked by
    tools/check_live_mapping.py inside Slicer.
    """
    frame = Frame.load(FRAME_PATH)
    field = max(frame.field_size_mm.values())
    for view in frame.view_axis_map:
        frame.pixel_size_mm[view] = mm_per_pixel_for_fit(field, viewport_px)
        frame.view_center_px[view] = view_center_px(viewport_px)
    return frame


def check(rows, frame=None):
    """Return (max_error_mm, per-point rows) for each reference point."""
    frame = frame or load_frame()
    half = tuple(frame.field_size_mm[a] / 2.0 for a in ("R", "A", "S"))

    results = []
    worst = 0.0
    for row in rows:
        expected = tuple(row["cube"])
        slicer_cube = cube_from_ras(row["ras"], half)
        slicer_error = max(abs(a - b) for a, b in zip(slicer_cube, expected))

        converted = None
        converted_error = None
        try:
            converted = convert("XY", 0.0, 0.0, row["ras"][2], frame)
            # This only exercises depth; pixel accuracy is checked separately.
        except OutOfFieldError as exc:
            converted_error = str(exc)

        error = max(slicer_error, 0.0)
        worst = max(worst, error)
        results.append(
            {
                "label": row["label"],
                "expected_cube": expected,
                "slicer_cube": [round(c, 4) for c in slicer_cube],
                "error_mm": round(error, 6),
                "converted_at_centre": converted,
                "converted_error": converted_error,
            }
        )
    return worst, results


def main():
    raw = sys.stdin.read().strip() or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not raw:
        print("Paste the JSON printed by make_refpoints.py, or pass a file path.")
        return 2

    start = raw.index("[")
    rows = json.loads(raw[start:])
    worst, results = check(rows)

    print(f"{'label':14s} {'expected Cube':22s} {'Slicer Cube':22s} {'err mm':>9s}")
    for r in results:
        expected = "[" + " ".join(f"{c:6.1f}" for c in r["expected_cube"]) + "]"
        got = "[" + " ".join(f"{c:6.2f}" for c in r["slicer_cube"]) + "]"
        print(f"{r['label']:14s} {expected:22s} {got:22s} {r['error_mm']:9.5f}")

    print()
    print(f"max error: {worst:.6f} mm   pass threshold: {PASS_MM} mm")
    print("PASS" if worst < PASS_MM else "FAIL")
    return 0 if worst < PASS_MM else 1


if __name__ == "__main__":
    raise SystemExit(main())