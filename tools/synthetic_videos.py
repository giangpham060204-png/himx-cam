"""Render the synthetic three-camera scene to MP4s with known truth.

The mock clips prove that pictures can travel; these clips prove that the
*video path* can measure. They are rendered from the same synthetic rig that
``synthetic_stream.py`` uses, but with a pinhole lens so the marker's visual
centre and the published projection matrices agree to the pixel.

Outputs, all in ``--out``:

    cam00_320.mp4, cam11_320.mp4, cam20_320.mp4
    camera-rig.json      three 3x4 projections in millimetres
    truth.json           the marker is at (0, 0, 0)
    marker-centres.json  the three marker centres on frame 0

Stream them with:

    uv run tools/camera_stream.py --videos logs/synthetic-videos \
        --calibration logs/synthetic-videos/camera-rig.json \
        --truth logs/synthetic-videos/truth.json

The marker is deliberately larger than the sandbox's 40 mm default so the
detector is reliable at the working distance; its centre is still the origin.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import cv2
import numpy as np


def _resolve_project_root(_globals=None, _env=None):
    """Find the checkout root without scanning the disk (same rule as the tools)."""
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
if str(PROJECT_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import camera_sandbox  # noqa: E402
import camera_stream  # noqa: E402
import synthetic_camera as cam  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import DEFAULT_MODULE, MODULES, optics_for  # noqa: E402
from targeting.rig import Rig  # noqa: E402

DEFAULT_FRAMES = 30
DEFAULT_FPS = 10.0
DEFAULT_MARKER_MM = 100.0


def generate(out_dir, *, optics=None, geometry=None, frames=DEFAULT_FRAMES,
             fps=DEFAULT_FPS, seed=7, marker_mm=DEFAULT_MARKER_MM,
             rig=None, frame=None):
    """Write the videos and their calibration/truth/click metadata.

    Returns a dictionary of paths and the known values, so tests do not have to
    re-derive what the generator already knows.
    """
    frames = int(frames)
    if frames <= 0:
        raise ValueError("--frames must be positive")
    fps = float(fps)
    if fps <= 0:
        raise ValueError("--fps must be positive")
    marker_mm = float(marker_mm)
    if marker_mm <= 0:
        raise ValueError("--marker-mm must be positive")
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    optics = optics or optics_for()
    geometry = geometry or camera_sandbox.RigGeometry(marker_mm=marker_mm)
    rig = rig if rig is not None else Rig()
    frame = frame if frame is not None else Frame.load(PROJECT_ROOT / "frame.json")
    box = camera_sandbox.Sandbox(
        optics=optics, geometry=geometry, rig=rig, frame=frame, seed=seed)
    K = cam.camera_matrix(optics)
    poses = [camera.source.pose for camera in box.cameras]
    rng = np.random.default_rng(seed)
    writers = []
    first_frames = []
    for index, name in enumerate(camera_stream.VIDEO_NAMES):
        path = out_dir / name
        writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps,
            (optics.pixels_x, optics.pixels_y), isColor=True)
        if not writer.isOpened():
            for earlier in writers:
                earlier.release()
            raise RuntimeError(
                f"OpenCV cannot write {path}; an MP4-capable build is required")
        writers.append(writer)
        first_frames.append(None)

    try:
        for _frame_index in range(frames):
            for index, (R, t) in enumerate(poses):
                gray = cam.render(optics, K, np.asarray(R), np.asarray(t), rng,
                                  k1=0.0, marker_mm=marker_mm)
                if first_frames[index] is None:
                    first_frames[index] = gray.copy()
                writers[index].write(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR))
    finally:
        for writer in writers:
            writer.release()

    box.frames = first_frames
    rig_path = camera_sandbox.write_rig_file(box, out_dir / "camera-rig.json")
    detector = cam.detector()
    pixels = []
    for index, image in enumerate(first_frames):
        try:
            corners = cam.detect_marker(image, detector)
        except RuntimeError as exc:
            raise RuntimeError(
                f"camera {index + 1}: the rendered marker is not readable: {exc}") from None
        centre = cam.diagonal_crossing(corners)
        pixels.append([float(centre[0]), float(centre[1])])

    truth_mm = [0.0, 0.0, 0.0]
    truth_path = camera_stream.write_json_atomic(out_dir / "truth.json", {
        "point_mm": truth_mm,
        "marker_mm": marker_mm,
        "source": "synthetic-video",
    })
    clicks_path = camera_stream.write_json_atomic(out_dir / "marker-centres.json", {
        "pixels": pixels,
        "frame_id": 0,
        "truth_mm": truth_mm,
    })
    return {
        "out_dir": out_dir,
        "videos": [out_dir / name for name in camera_stream.VIDEO_NAMES],
        "rig": rig_path,
        "truth": truth_path,
        "clicks": clicks_path,
        "pixels": pixels,
        "truth_mm": truth_mm,
        "frame_id": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(PROJECT_ROOT / "logs" / "synthetic-videos"),
                        help="output folder for the three clips and metadata")
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--marker-mm", type=float, default=DEFAULT_MARKER_MM)
    parser.add_argument("--optics", default=DEFAULT_MODULE,
                        choices=sorted(MODULES))
    args = parser.parse_args(argv)
    try:
        result = generate(
            args.out, optics=optics_for(args.optics), frames=args.frames,
            fps=args.fps, seed=args.seed, marker_mm=args.marker_mm)
    except (RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"wrote {len(result['videos'])} clips, {result['rig']}, "
          f"{result['truth']}, and {result['clicks']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
