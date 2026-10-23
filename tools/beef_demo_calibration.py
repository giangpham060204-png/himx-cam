"""Generate the estimated demo calibration for the downloaded beef clips.

This is not a measurement calibration. It converts the LLFF poses to 320-pixel
projections, chooses a miniature metric scale that fits the 100 mm field, and
marks the result demo-only. The Start video dialog can generate it too; this
tool is for a different scale or for inspecting the conversion.

    uv run tools/beef_demo_calibration.py
    uv run tools/beef_demo_calibration.py --scale-mm-per-unit 4.0
"""

from __future__ import annotations

import argparse
import pathlib
import sys

# No hardcoded fallback: panic instead of guessing (same rule as the tools).


def _resolve_project_root(_globals=None, _env=None):
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
        f"to the directory containing frame.json. __file__={here!r}")


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from targeting.beef_demo import BeefDemoError, write_demo_calibration  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--poses",
        default=str(PROJECT_ROOT / "data" / "multiview-beef" / "poses_bounds.npy"),
        help="the Neural 3D Video poses_bounds.npy",
    )
    parser.add_argument(
        "--out",
        default=str(PROJECT_ROOT / "logs" / "beef-demo" / "camera-rig.json"),
        help="where to write the demo camera-rig.json",
    )
    parser.add_argument("--scale-mm-per-unit", type=float, default=4.0,
                        help="demo millimetres per normalized LLFF unit")
    parser.add_argument("--max-residual-px", type=float, default=20.0,
                        help="demo-only residual limit for the approximate poses")
    parser.add_argument("--filtered-camera", type=int, default=4,
                        help="the camera id missing from poses_bounds.npy")
    parser.add_argument("--cameras", nargs=3, type=int, default=(0, 11, 20),
                        metavar=("C1", "C2", "C3"))
    args = parser.parse_args(argv)

    try:
        payload = write_demo_calibration(
            args.poses, args.out,
            scale_mm_per_unit=args.scale_mm_per_unit,
            max_residual_px=args.max_residual_px,
            filtered_camera=args.filtered_camera,
            camera_ids=tuple(args.cameras),
        )
    except (BeefDemoError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"wrote demo calibration {args.out}")
    print(f"  cameras: {payload['camera_ids']}")
    print(f"  pose rows: {payload['pose_rows']}")
    print(f"  scale: {payload['scale_mm_per_unit']:g} mm per LLFF unit")
    print(f"  residual limit: {payload['max_residual_px']:g} px")
    print(f"  warning: {payload['warning']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
