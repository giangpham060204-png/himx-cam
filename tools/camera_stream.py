"""Send three camera frames to Slicer, solve click sets while the video plays.

The mock videos and the future HM01B0 cameras differ only below the
``FrameSource`` seam. This tool owns capture, pacing, looping, the network
send, the frame/pause state, and the calibrated click solve. Slicer receives
three image nodes named ``c1``/``c2``/``c3`` and publishes three pixels plus
the frame they belong to.

Run it with Slicer already listening:

    uv run tools/camera_stream.py --videos data/multiview-beef

With three explicit clips, in camera order:

    uv run tools/camera_stream.py --video-files c1.mp4 c2.mp4 c3.mp4

With a calibration (the same ``camera-rig.json`` the synthetic path writes):

    uv run tools/camera_stream.py --videos data/multiview-beef \
        --calibration path/to/camera-rig.json

A synthetic video with a known point carries its own truth error:

    uv run tools/synthetic_videos.py --out logs/synthetic-videos
    uv run tools/camera_stream.py --videos logs/synthetic-videos \
        --calibration logs/synthetic-videos/camera-rig.json \
        --truth logs/synthetic-videos/truth.json

Future UVC cameras (Pico HM01B0 at 160x120 needs ``--resize``):

    uv run tools/camera_stream.py --devices /dev/video0 /dev/video2 /dev/video4 \
        --resize --calibration path/to/camera-rig.json

Slicer side: install SlicerOpenIGTLink, press *Connect camera stream*, then
click the same fixed target once in each view. The video keeps playing while
the clicks are solved; press *Pause stream* first when the target, a camera, or
an occluder can move between clicks. Without a calibration a complete click set
is reported as uncalibrated; it never invents a point. A demo calibration may
set ``"demo": true`` and a larger ``max_residual_px``; its results are marked
DEMO and are not written to the camera log.

Images are sent as one axial slice per camera, shape ``(1, H, W)``. A 2-D array
is interpreted by OpenIGTLink as a single sagittal slice, which Slicer's axial
views show as a one-pixel line.
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

import click_solver  # noqa: E402
import frame_sources  # noqa: E402
from targeting.camera_log import append_camera_target  # noqa: E402
from targeting.clicks import CLICKS_FILE, read_clicks  # noqa: E402
from targeting.frame import Frame  # noqa: E402
from targeting.optics import DEFAULT_MODULE, MODULES, optics_for  # noqa: E402
from targeting.rig import Rig, load_rig  # noqa: E402

DEVICE_NAMES = ("c1", "c2", "c3")
VIDEO_NAMES = ("cam00_320.mp4", "cam11_320.mp4", "cam20_320.mp4")
DEFAULT_PORT = 18944
DEFAULT_FPS = 30.0
STREAM_FOLDER = PROJECT_ROOT / "logs" / "camera-stream"
STREAM_STATE_FILE = STREAM_FOLDER / "stream-state.json"
STREAM_COMMAND_FILE = STREAM_FOLDER / "stream-command.json"
STREAM_RIG_FILE = STREAM_FOLDER / "camera-rig.json"
STREAM_RESULT_FILE = STREAM_FOLDER / "result.json"


def write_json_atomic(path, payload):
    """Write a small JSON document atomically; Slicer polls these files."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def video_paths(folder):
    """The three expected clips, or a refusal naming the missing ones.

    The directory is explicit and the names are fixed: a stream that silently
    took a differently named file would show the wrong camera in the wrong
    pane, which is worse than refusing to start.
    """
    folder = pathlib.Path(folder)
    paths = tuple(folder / name for name in VIDEO_NAMES)
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"{folder} is missing {', '.join(missing)}; expected the three "
            f"320x320 clips: {', '.join(VIDEO_NAMES)}"
        )
    return paths


def video_paths_from_files(paths):
    """Three explicitly chosen clips, in camera order, or a refusal."""
    files = tuple(pathlib.Path(path) for path in paths)
    if len(files) != 3:
        raise ValueError("--video-files needs exactly three clips (C1, C2, C3)")
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "missing video file: " + ", ".join(missing))
    return files


def open_sources(paths, optics):
    """One VideoFileSource per clip, in camera order."""
    return [frame_sources.VideoFileSource(path, optics) for path in paths]


def _require_pyigtl():
    try:
        import pyigtl
    except ImportError as exc:
        raise RuntimeError(
            "pyigtl is not installed; run `uv sync` in the project checkout"
        ) from exc
    return pyigtl


def image_message(device, frame, timestamp):
    """One axial slice, with the camera name and the shared frame number.

    OpenIGTLink image axes are (k, j, i). A camera frame is (row, column), so
    it becomes (1, row, column): one axial slice, not one sagittal slice.
    """
    pyigtl = _require_pyigtl()
    image = np.asarray(frame)
    if image.ndim == 2:
        image = image[np.newaxis, :, :]
    if image.ndim != 3:
        raise ValueError(f"a camera frame must be 2-D, got shape {image.shape}")
    if image.dtype != np.uint8:
        raise ValueError(f"a camera frame must be uint8, got {image.dtype}")
    # The PNG path in Slicer uses the same two flips (R and A). Without them an
    # axial slice arrives rotated 180 degrees: the picture is still there, but
    # every click would mean a different pixel than the camera saw.
    ijk_to_ras = np.diag([-1.0, -1.0, 1.0, 1.0])
    return pyigtl.ImageMessage(
        image,
        device_name=str(device),
        timestamp=float(timestamp),
        world_coordinate_system="ras",
        ijk_to_world_matrix=ijk_to_ras,
    )


class PyIgtlLink:
    """A reconnecting pyigtl client to Slicer's OpenIGTLinkIF server."""

    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, timeout=30.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.clock = clock
        self.sleep = sleep
        self.client = None
        self._pyigtl = _require_pyigtl()
        self.last_error = None

    def _open(self):
        try:
            self.client = self._pyigtl.OpenIGTLinkClient(self.host, self.port)
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - retried by the caller
            self.client = None
            self.last_error = exc

    def wait_until_connected(self, timeout=None):
        """Block until Slicer's server accepts the connection, or time out."""
        timeout = self.timeout if timeout is None else float(timeout)
        deadline = self.clock() + timeout
        while True:
            if self.client is None or not self.client.is_connected():
                self.close()
                self._open()
            if self.client is not None and self.client.is_connected():
                return
            if self.clock() >= deadline:
                detail = f" ({self.last_error})" if self.last_error else ""
                raise RuntimeError(
                    f"no Slicer OpenIGTLink server at {self.host}:{self.port} "
                    f"after {timeout:g}s{detail}; press Connect camera stream "
                    "in Slicer first"
                )
            self.sleep(0.2)

    def send(self, device, frame, timestamp):
        """Send one camera frame, reconnecting once if the server restarted."""
        if self.client is None or not self.client.is_connected():
            self.wait_until_connected()
        try:
            self.client.send_message(image_message(device, frame, timestamp))
        except Exception:  # noqa: BLE001 - one reconnect attempt, then raise
            self.close()
            self.wait_until_connected()
            self.client.send_message(image_message(device, frame, timestamp))

    def close(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001 - closing must not mask a send error
                pass


class CameraStream:
    """Read the three sources in step and hand each frame to the link."""

    def __init__(self, sources, link, fps=DEFAULT_FPS, loop=True,
                 source_factory=None, clock=time.monotonic, sleep=time.sleep):
        self.sources = list(sources)
        self.link = link
        self.fps = float(fps)
        self.loop = bool(loop)
        self.source_factory = source_factory
        self.clock = clock
        self.sleep = sleep
        self.frame_index = 0

    def _grab_all(self):
        try:
            return [source.grab() for source in self.sources]
        except RuntimeError:
            if not self.loop:
                return None
            if self.source_factory is None:
                raise
            self.sources = list(self.source_factory())
            return [source.grab() for source in self.sources]

    def tick(self):
        """Send one frame from every camera; return the frame number or None."""
        frames = self._grab_all()
        if frames is None:
            return None
        index = self.frame_index
        for device, frame in zip(DEVICE_NAMES, frames):
            self.link.send(device, frame, index)
        self.frame_index += 1
        return index

    def run(self, max_frames=None):
        """Send frames until the sources end, the caller stops, or the limit."""
        sent = 0
        start = self.clock()
        while max_frames is None or sent < max_frames:
            index = self.tick()
            if index is None:
                break
            sent += 1
            if self.fps > 0:
                target = start + sent / self.fps
                delay = target - self.clock()
                if delay > 0:
                    self.sleep(delay)
        return sent


class ClickableVideoStream(CameraStream):
    """A stream that solves complete click sets without pausing the video.

    The three clicks are pixel observations; the calibrated camera matrices
    turn them into rays. A fixed target stays on the same rays across frames,
    so the video can keep playing. Use the manual pause controls when the
    target or a camera can move, or when a moving object occludes the target.
    Clearing the clicks still resumes a manually paused stream.
    """

    def __init__(self, sources, link, fps=DEFAULT_FPS, loop=True,
                 source_factory=None, logs_dir=None, calibration=None,
                 rig=None, frame=None, truth=None,
                 clock=time.monotonic, sleep=time.sleep):
        super().__init__(sources, link, fps=fps, loop=loop,
                         source_factory=source_factory, clock=clock, sleep=sleep)
        self.logs_dir = pathlib.Path(logs_dir) if logs_dir else PROJECT_ROOT / "logs"
        self.stream_dir = self.logs_dir / "camera-stream"
        self.stream_dir.mkdir(parents=True, exist_ok=True)
        self.calibration = calibration
        self.rig = rig if rig is not None else Rig()
        self.frame = frame
        self.truth = tuple(float(value) for value in truth) if truth is not None else None
        if calibration is not None and frame is None:
            raise ValueError("a calibrated stream needs a Frame for the field check")
        self.paused = False
        self.last_frames = []
        self.last_click_mtime = None
        self.last_command_mtime = None
        self.session_start = time.time()
        self.last_outcome = None
        self.write_state()

    def write_state(self):
        """Publish the frame the images belong to and whether clicks froze it."""
        write_json_atomic(self.stream_dir / STREAM_STATE_FILE.name, {
            "frame_id": self.frame_index - 1 if self.frame_index else None,
            "paused": bool(self.paused),
            "calibrated": self.calibration is not None,
            "source": "video",
            "when": datetime.now().isoformat(timespec="seconds"),
        })

    def _grab_all(self):
        """A live camera failure is reported; a recorded clip simply ends."""
        try:
            return [source.grab() for source in self.sources]
        except RuntimeError:
            if not self.loop and self.source_factory is None:
                raise
            return super()._grab_all()

    def _prepare_frames(self, frames):
        if frames is None or self.calibration is None:
            return frames
        return [self.calibration.undistort(index, frame)
                for index, frame in enumerate(frames)]

    def tick(self):
        """Send one complete frame set, unless a click froze the stream."""
        if self.paused:
            return None
        frames = self._prepare_frames(self._grab_all())
        if frames is None:
            return None
        index = self.frame_index
        for device, frame in zip(DEVICE_NAMES, frames):
            self.link.send(device, frame, index)
        self.last_frames = list(frames)
        self.frame_index += 1
        self.write_state()
        return index

    def _frames_for(self, clicks):
        """The frame set for a click solve.

        A live solve uses the frames already on screen and leaves the stream
        running. A manually paused stream can seek to the recorded click frame,
        which is the exact snapshot the operator inspected.
        """
        if not self.paused:
            return (list(self.last_frames),
                    self.frame_index - 1 if self.frame_index else None)
        frame_id = clicks.frame_id
        if frame_id is None:
            frame_id = self.frame_index - 1 if self.frame_index else None
        if frame_id is not None:
            seekers = [getattr(source, "seek", None) for source in self.sources]
            if all(callable(seek) for seek in seekers):
                try:
                    for source in self.sources:
                        source.seek(frame_id)
                    frames = self._prepare_frames(
                        [source.grab() for source in self.sources])
                    self.frame_index = int(frame_id) + 1
                    self.last_frames = list(frames)
                    return frames, int(frame_id)
                except Exception as exc:  # noqa: BLE001 - fall back to the frozen set
                    print(f"could not seek to frame {frame_id}: {exc}", flush=True)
        return list(self.last_frames), (self.frame_index - 1 if self.frame_index else None)

    def solve(self, clicks):
        """Solve one complete click set, or refuse it with the current calibration."""
        frames, frame_id = self._frames_for(clicks)
        if len(frames) != len(DEVICE_NAMES):
            return None
        if self.calibration is None:
            measurement = click_solver.Measurement(
                status="uncalibrated",
                message=("no --calibration camera-rig.json; the click set is "
                         "not solved"),
                pixels=tuple(clicks.pixels),
            )
        else:
            measurement = click_solver.solve_click_set(
                self.calibration.projections, clicks.pixels, self.rig, self.frame,
                truth=self.truth,
                max_residual_px=self.calibration.max_residual_px)

        log_error = None
        demo = bool(self.calibration and self.calibration.demo)
        if measurement.status == "found" and not demo:
            residual = measurement.residual_px
            try:
                append_camera_target(
                    self.logs_dir,
                    cube=measurement.cube,
                    ras=measurement.ras,
                    residual_px=float(sum(residual) / len(residual)),
                    pixels=measurement.pixels,
                    source="video",
                    found_by="manual",
                )
            except OSError as exc:
                log_error = str(exc)
        elif measurement.status == "found":
            log_error = "demo calibration: not written to the camera log"
        payload = {
            "when": datetime.now().isoformat(timespec="seconds"),
            "frame": frame_id,
            "status": measurement.status,
            "message": measurement.message,
            "pixels": [list(pixel) for pixel in measurement.pixels],
            "cube": list(measurement.cube),
            "ras": list(measurement.ras),
            "rig_point": list(measurement.rig_point),
            "residual_px": list(measurement.residual_px),
            "error_mm": measurement.error_mm,
            "predicted_mm": None,
            "truth_mm": list(self.truth) if self.truth is not None else None,
            "source": "video",
            "calibrated": self.calibration is not None,
            "demo": demo,
            "log_error": log_error,
        }
        write_json_atomic(self.stream_dir / STREAM_RESULT_FILE.name, payload)
        self.last_outcome = measurement
        if measurement.status == "found":
            print(f"click set solved on frame {frame_id}: residuals "
                  f"{', '.join(f'{value:.2f}' for value in measurement.residual_px)} px",
                  flush=True)
        else:
            print(f"click set {measurement.status}: {measurement.message}", flush=True)
        self.write_state()
        return measurement

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

    def poll_clicks(self):
        """Solve a new complete click set; clearing the set resumes the stream."""
        path = self.logs_dir / CLICKS_FILE
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_mtime < self.session_start:
            return None
        if stat.st_mtime_ns == self.last_click_mtime:
            return None
        clicks, _note = read_clicks(path)
        if clicks is None:
            return None
        self.last_click_mtime = stat.st_mtime_ns
        if all(pixel is None for pixel in clicks.pixels):
            if self.paused:
                self.paused = False
                self.write_state()
                print("clicks cleared; stream resumed", flush=True)
            return None
        if clicks.frame_id is None:
            # A click set from a PNG snapshot (or a stream that has not
            # published a frame yet) is not a click on this video.
            return None
        if not clicks.is_complete():
            return None
        return self.solve(clicks)

    def run(self, max_frames=None):
        """Stream continuously; solve complete click sets as they arrive."""
        sent = 0
        start = self.clock()
        while max_frames is None or sent < max_frames:
            self.poll_command()
            if not self.paused:
                index = self.tick()
                if index is None:
                    break
                sent += 1
            self.poll_clicks()
            if self.paused:
                self.sleep(0.05)
            elif self.fps > 0:
                target = start + sent / self.fps
                delay = target - self.clock()
                if delay > 0:
                    self.sleep(delay)
        return sent


def _publish_calibration(path):
    """Copy a validated calibration beside the stream for Slicer's rays."""
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    STREAM_FOLDER.mkdir(parents=True, exist_ok=True)
    write_json_atomic(STREAM_RIG_FILE, payload)


def load_truth(path):
    """Read a known rig point from ``truth.json`` for a synthetic-video run.

    A real camera run has no truth file; passing one is optional and only adds
    an error column. A malformed file is refused rather than interpreted as an
    origin, which would turn every later result into a false "correct".
    """
    path = pathlib.Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read the truth file {path}: {exc}") from None
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from None
    if isinstance(payload, dict):
        payload = payload.get("point_mm", payload.get("point"))
    if not isinstance(payload, (list, tuple)) or len(payload) != 3:
        raise ValueError(f"{path}: truth must be three millimetre coordinates")
    try:
        point = tuple(float(value) for value in payload)
    except (TypeError, ValueError):
        raise ValueError(f"{path}: truth must be three numbers") from None
    if not all(np.isfinite(value) for value in point):
        raise ValueError(f"{path}: truth coordinates must be finite")
    return point


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--videos",
        default=None,
        help="folder holding cam00_320.mp4, cam11_320.mp4 and cam20_320.mp4",
    )
    source.add_argument(
        "--video-files", nargs=3, default=None, metavar=("C1", "C2", "C3"),
        help="three explicit clips, in camera order",
    )
    parser.add_argument("--devices", nargs="+", default=None,
                        help="three UVC device indices/paths; overrides the video source")
    parser.add_argument("--resize", action="store_true",
                        help="resize a smaller UVC stream (Pico 160x120) to the optics size")
    parser.add_argument("--calibration", default=None,
                        help="camera-rig.json with three calibrated projections")
    parser.add_argument("--truth", default=None,
                        help="truth.json with the known point, for synthetic-video runs")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS,
                        help="send rate; the clips are 30 fps")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="seconds to wait for Slicer's OpenIGTLink server")
    parser.add_argument("--no-loop", action="store_true",
                        help="stop at the end of the 10-second clips")
    parser.add_argument("--max-frames", type=int, default=None,
                        help="stop after N frames (for smoke tests)")
    parser.add_argument("--optics", default=DEFAULT_MODULE,
                        choices=sorted(MODULES))
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    video_folder = args.videos or str(
        PROJECT_ROOT / "data" / "multiview-beef")

    optics = optics_for(args.optics)
    try:
        frame = Frame.load(PROJECT_ROOT / "frame.json")
        rig = load_rig(PROJECT_ROOT / "rig.json")
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    calibration = None
    if args.calibration:
        try:
            calibration = click_solver.load_calibration(args.calibration)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        if (calibration.width, calibration.height) != (optics.pixels_x,
                                                       optics.pixels_y):
            print(
                f"{args.calibration}: calibration is "
                f"{calibration.width}x{calibration.height}, but the stream is "
                f"{optics.pixels_x}x{optics.pixels_y}",
                file=sys.stderr)
            return 1
        _publish_calibration(args.calibration)
    else:
        try:
            STREAM_RIG_FILE.unlink(missing_ok=True)
        except OSError as exc:
            print(f"cannot clear the old stream calibration: {exc}",
                  file=sys.stderr)
            return 1

    truth = None
    if args.truth:
        try:
            truth = load_truth(args.truth)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1

    try:
        if args.devices:
            if len(args.devices) != 3:
                raise ValueError("--devices needs exactly three cameras")
            sources = [frame_sources.CameraSource(device, optics,
                                                  resize=args.resize)
                       for device in args.devices]
            factory = None
            loop = False
            source_label = "devices " + ", ".join(str(device) for device in args.devices)
        else:
            if args.video_files:
                paths = video_paths_from_files(args.video_files)
                source_label = "C1={}, C2={}, C3={}".format(
                    *(str(path) for path in paths))
            else:
                paths = video_paths(video_folder)
                source_label = str(pathlib.Path(video_folder))

            def factory():
                return open_sources(paths, optics)

            sources = factory()
            loop = not args.no_loop
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1

    try:
        link = PyIgtlLink(host=args.host, port=args.port, timeout=args.timeout)
        stream = ClickableVideoStream(
            sources, link, fps=args.fps, loop=loop,
            source_factory=factory, logs_dir=PROJECT_ROOT / "logs",
            calibration=calibration, rig=rig, frame=frame, truth=truth,
        )
    except (RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if calibration is not None and calibration.demo:
        calibration_note = (
            "DEMO: estimated scale and approximate calibration; not a "
            "measurement")
    elif calibration is not None:
        calibration_note = "calibrated; complete clicks will solve"
    else:
        calibration_note = (
            "uncalibrated; complete clicks are refused and no point is solved")
    if truth is not None:
        calibration_note += f"; truth at {truth}"
    print(f"streaming {', '.join(DEVICE_NAMES)} from {source_label} "
          f"to {args.host}:{args.port} at {args.fps:g} fps ({calibration_note})",
          flush=True)
    try:
        link.wait_until_connected()
        print("connected to Slicer", flush=True)
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
