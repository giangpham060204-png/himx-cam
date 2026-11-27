"""Fit and use the rigid virtual-to-real marker registration.

The registration file holds the same markers in two frames: ``virtual_mm`` is
where they sit in the Slicer cube, ``real_mm`` is the measured real frame. This
tool is the console side of the panel's Virtual -> real section.

    uv run tools/register_markers.py --check
    uv run tools/register_markers.py --map 30 20 0
    uv run tools/register_markers.py --json

A fit writes the solved transform and the per-marker residual to
``logs/registration-solved.json``. ``--check`` exits non-zero when the
residual is above the limit, so a script can refuse a bad measurement.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from targeting import registration  # noqa: E402


def write_json_atomic(path, payload):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
    return path


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registration", default=str(registration.DEFAULT_PATH),
                        help="marker-registration.json to read")
    parser.add_argument("--map", nargs=3, type=float, metavar=("X", "Y", "Z"),
                        help="map one virtual cube point to the real frame")
    parser.add_argument("--json", action="store_true",
                        help="print the solved fit as JSON")
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero when the residual is above the limit")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = registration.load_registration(args.registration)
        fit = registration.fit_registration(config)
    except (RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1

    payload = registration.fit_payload(fit, config)
    payload["when"] = datetime.now().isoformat(timespec="seconds")
    solved = write_json_atomic(PROJECT_ROOT / "logs" / "registration-solved.json",
                               payload)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(registration.fit_note(fit))
        print(f"solved map written to {solved}")
    if args.map is not None:
        point = registration.apply_transform(fit, args.map)
        print("virtual ({:.2f}, {:.2f}, {:.2f}) mm -> real ({:.2f}, {:.2f}, "
              "{:.2f}) mm in {!r}".format(*args.map, *point, fit["real_frame"]))

    if args.check and (fit["rms"] > registration.MAX_RESIDUAL_MM
                       or abs(fit["scale"] - 1.0)
                       > registration.MAX_SCALE_ERROR):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
