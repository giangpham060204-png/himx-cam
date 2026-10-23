"""Stream the synthetic three-camera scene to Slicer and solve its clicks.

This is Stage 2A's ground-truth path. The mock videos prove the pictures can
travel; this tool proves the *whole* measurement path with a point whose true
position is known:

    synthetic frames -> OpenIGTLink -> Slicer clicks
    -> targeting/triangulation.py -> camera log -> Slicer 3D view

The marker sits at the rig origin, so every solved point can be compared with
truth instead of being accepted because it looks plausible. The same click and
log path will carry the calibrated HM01B0 cameras later.

Run it with Slicer already listening:

    uv run tools/synthetic_stream.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime

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
        "to the directory containing frame.json, or set PROJECT_ROOT_OVERRIDE "
        f"in the Slicer console before exec(). __file__={here!r}"
    )


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import camera_sandbox  # noqa: E402
import camera_stream  # noqa: E402
from targeting.clicks import CLICKS_FILE, read_clicks  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import DEFAULT_MODULE, MODULES, optics_for  # noqa: E402
from targeting.rig import load_rig  # noqa: E402

STREAM_FOLDER = PROJECT_ROOT / "logs" / "camera-stream"
RIG_FILE = STREAM_FOLDER / "camera-rig.json"
TRUTH_FILE = STREAM_FOLDER / "truth.json"
RESULT_FILE = STREAM_FOLDER / "result.json"
DEFAULT_FPS = 5.0
TRUTH_POINT = (0.0, 0.0, 0.0)


def write_json(path, payload):
    """Write a small JSON document atomically; Slicer polls these files."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def result_payload(box, outcome, frame_index, *, log_error=None):
    """Everything the Slicer panel needs to judge one solved click set."""
    rig_point = None
    if outcome.status == "found":
        rig_point = tuple(float(value) for value in box.rig.to_rig(outcome.ras))
    return {
        "when": datetime.now().isoformat(timespec="seconds"),
        "frame": int(frame_index),
        "status": outcome.status,
        "message": outcome.message,
        "pixels": [list(pixel) for pixel in outcome.pixels],
        "cube": list(outcome.cube),
        "ras": list(outcome.ras),
        "rig_point": list(rig_point) if rig_point is not None else None,
        "residual_px": list(outcome.residual_px),
        "error_mm": outcome.error_mm,
        "predicted_mm": outcome.predicted_mm,
        "truth_mm": list(TRUTH_POINT),
        "source": "synthetic",
        "log_error": log_error,
    }


class SyntheticStream:
    """Render the known scene, send it, and solve any complete click set."""

    def __init__(self, box, link, fps=DEFAULT_FPS, logs_dir=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.box = box
        self.link = link
        self.fps = float(fps)
        self.logs_dir = pathlib.Path(logs_dir) if logs_dir else PROJECT_ROOT / "logs"
        self.stream_dir = self.logs_dir / "camera-stream"
        self.stream_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.sleep = sleep
        self.frame_index = 0
        self.last_frames = []
        self.last_click_mtime = None
        self.last_command_mtime = None
        self.paused = False
        self.session_start = time.time()
        self.last_outcome = None
        self.write_state()

    def write_state(self):
        """Keep the frame/pause contract even though the synthetic scene is static."""
        write_json(self.stream_dir / "stream-state.json", {
            "frame_id": self.frame_index - 1 if self.frame_index else None,
            "paused": bool(self.paused),
            "calibrated": True,
            "source": "synthetic",
            "when": datetime.now().isoformat(timespec="seconds"),
        })

    def poll_command(self):
        """Pause or resume when Slicer writes a stream command."""
        path = self.stream_dir / "stream-command.json"
        try:
            stat = path.stat()
        except OSError:
            return
        if stat.st_mtime < self.session_start:
            return
        if stat.st_mtime_ns == self.last_command_mtime:
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.last_command_mtime = stat.st_mtime_ns
        command = payload.get("command")
        if command == "pause" and not self.paused:
            self.paused = True
            self.write_state()
            print(f"paused by Slicer at frame {self.frame_index - 1}", flush=True)
        elif command == "step" and self.paused:
            self.paused = False
            self.tick()
            self.paused = True
            self.write_state()
            print(f"stepped to frame {self.frame_index - 1}", flush=True)
        elif command == "resume" and self.paused:
            self.paused = False
            self.write_state()
            print("resumed by Slicer", flush=True)

    def tick(self):
        """Send one frame from each synthetic camera; return its frame number."""
        if self.paused:
            return None
        frames = self.box.render_all()
        for device, frame in zip(camera_stream.DEVICE_NAMES, frames):
            self.link.send(device, frame, self.frame_index)
        self.last_frames = list(frames)
        index = self.frame_index
        self.frame_index += 1
        self.write_state()
        return index

    def poll_clicks(self):
        """Solve a newly published, complete set; ignore stale or partial ones."""
        path = self.logs_dir / CLICKS_FILE
        try:
            mtime = path.stat().st_mtime_ns
            if path.stat().st_mtime < self.session_start:
                return None
        except OSError:
            return None
        if mtime == self.last_click_mtime or not self.last_frames:
            return None

        clicks, _note = read_clicks(path)
        if clicks is None or not clicks.is_complete():
            return None
        self.last_click_mtime = mtime
        self.box.frames = list(self.last_frames)
        outcome = self.box.nominate_manually(clicks.pixels)
        log_error = None
        if outcome.status == "found":
            try:
                self.box.place(self.logs_dir)
            except Exception as exc:  # noqa: BLE001 - reported in the result
                log_error = str(exc)
        result = result_payload(self.box, outcome, self.frame_index - 1,
                                log_error=log_error)
        write_json(self.stream_dir / RESULT_FILE.name, result)
        self.last_outcome = outcome
        if outcome.status == "found":
            print(f"click set solved: error {outcome.error_mm:.3f} mm "
                  f"against truth, residuals "
                  f"{', '.join(f'{value:.2f}' for value in outcome.residual_px)} px",
                  flush=True)
        else:
            print(f"click set refused: {outcome.status}: {outcome.message}",
                  flush=True)
        self.write_state()
        return outcome

    def run(self, max_frames=None):
        """Stream and solve until interrupted or the optional frame limit."""
        sent = 0
        start = self.clock()
        while max_frames is None or sent < max_frames:
            self.poll_command()
            if not self.paused:
                self.tick()
                sent += 1
            self.poll_clicks()
            if self.paused:
                self.sleep(0.05)
                continue
            if self.fps > 0:
                target = start + sent / self.fps
                delay = target - self.clock()
                if delay > 0:
                    self.sleep(delay)
        return sent


def write_metadata(box, stream_dir):
    """Publish the projection matrices and the known truth before streaming."""
    stream_dir = pathlib.Path(stream_dir)
    stream_dir.mkdir(parents=True, exist_ok=True)
    camera_sandbox.write_rig_file(box, stream_dir / "camera-rig.json")
    write_json(stream_dir / "truth.json", {
        "point_mm": list(TRUTH_POINT),
        "marker_mm": float(box.geometry.marker_mm),
        "source": "synthetic",
        "when": datetime.now().isoformat(timespec="seconds"),
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS,
                        help="render/send rate; the synthetic scene is static")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=camera_stream.DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds to wait for Slicer's OpenIGTLink server")
    parser.add_argument("--max-frames", type=int, default=None,
                        help="stop after N frames (for smoke tests)")
    parser.add_argument("--spread", type=float, default=150.0)
    parser.add_argument("--standoff", type=float, default=500.0)
    parser.add_argument("--aim", type=float, default=35.0)
    parser.add_argument("--marker-mm", type=float, default=40.0)
    parser.add_argument("--optics", default=DEFAULT_MODULE,
                        choices=sorted(MODULES))
    args = parser.parse_args(argv)

    try:
        optics = optics_for(args.optics)
        box = camera_sandbox.Sandbox(
            optics=optics,
            geometry=camera_sandbox.RigGeometry(
                spread=args.spread, standoff=args.standoff,
                aim_offset=args.aim, marker_mm=args.marker_mm),
            rig=load_rig(PROJECT_ROOT / "rig.json"),
            frame=Frame.load(PROJECT_ROOT / "frame.json"),
        )
        link = camera_stream.PyIgtlLink(
            host=args.host, port=args.port, timeout=args.timeout)
        stream = SyntheticStream(box, link, fps=args.fps)
        write_metadata(box, STREAM_FOLDER)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"synthetic truth at {TRUTH_POINT} mm; streaming c1, c2, c3 "
          f"to {args.host}:{args.port} at {args.fps:g} fps", flush=True)
    try:
        link.wait_until_connected()
        print("connected to Slicer; click the marker centre in all three views",
              flush=True)
        sent = stream.run(max_frames=args.max_frames)
        print(f"sent {sent} frame(s)", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        return 0
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())
