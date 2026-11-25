"""Where the pictures come from -- the one thing that changes with hardware.

Everything above this file works from a camera's projection matrix and the pixel
it reported. Nothing above it may know whether those pixels were rendered or
photographed, and that is what lets the sandbox drive real cameras later without
the maths, the window or the log changing at all.

So the seam is deliberately thin: a source hands over one frame, and the record
that travels with it carries the calibration. The simulation fabricates that
calibration and real hardware measures it; both hand over the same shape.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from targeting.optics import Optics

from synthetic_camera import MARKER_MM, camera_matrix, render


class FrameSource(Protocol):
    """One camera's frames, however the camera is made."""

    def grab(self) -> np.ndarray:
        """A uint8 frame, shape (pixels_y, pixels_x), monochrome."""


# eq=False: the dataclass would otherwise try to compare numpy arrays field by
# field, which raises instead of returning a bool.
@dataclass(frozen=True, eq=False)
class Camera:
    """A camera as everything above the seam sees it."""

    name: str
    optics: Optics
    projection: np.ndarray          # 3x4, K [R|t]
    source: FrameSource


class SyntheticSource:
    """Frames from the renderer: the same HM01B0, drawn instead of photographed."""

    def __init__(self, optics, pose, rng, marker_mm=MARKER_MM):
        self.optics = optics
        self.pose = pose            # (R, t) in the rig frame
        self.rng = rng
        self.marker_mm = marker_mm

    def grab(self):
        R, t = self.pose
        return render(self.optics, camera_matrix(self.optics), R, t, self.rng,
                      marker_mm=self.marker_mm)


class StoredFrames:
    """Frames that were rendered once and kept -- a recorded clip, in effect.

    This is what makes the seam testable: the same pictures arriving from
    storage instead of from the renderer must give the same answer.
    """

    def __init__(self, frames):
        self.frames = list(frames)
        self.given = 0

    def grab(self):
        if self.given >= len(self.frames):
            raise RuntimeError("the stored frames ran out")
        frame = self.frames[self.given]
        self.given += 1
        return frame


class VideoFileSource:
    """Frames from a recorded clip, so a phone camera can stand in."""

    def __init__(self, path, optics):
        self.path = Path(path)
        self.optics = optics
        self.capture = cv2.VideoCapture(str(self.path))
        if not self.capture.isOpened():
            raise RuntimeError(f"cannot open the video {self.path}")

    def grab(self):
        ok, frame = self.capture.read()
        if not ok:
            raise RuntimeError(f"the video {self.path} ran out of frames")
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        expected = (self.optics.pixels_y, self.optics.pixels_x)
        if grey.shape != expected:
            raise RuntimeError(
                f"{self.path} gives {grey.shape} frames, but the camera is "
                f"{expected}; rescale the clip or fix the optics"
            )
        return grey

    def seek(self, frame_index):
        """Move to a recorded frame so a click can be solved on that frame.

        A frame number is a property of a file, not of a live camera: only
        this source offers it. The next ``grab`` returns that frame and then
        continues forward from there.
        """
        frame_index = int(frame_index)
        if frame_index < 0:
            raise ValueError("a frame number cannot be negative")
        if not self.capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index):
            raise RuntimeError(
                f"cannot seek {self.path} to frame {frame_index}")
        return frame_index


class CameraSource:
    """A real camera. Written now so the shape is fixed; inert without hardware.

    ``resize`` is for the Pico's 160x120 UVC stream: the HM01B0 sensor is
    320x320, but the standard USB path exposes a quarter of it. Resizing makes
    the calibration file describe the pixels the rest of the program sees.
    """

    def __init__(self, device, optics, resize=False):
        self.device = device
        self.optics = optics
        self.resize = bool(resize)
        self.capture = None

    def grab(self):
        if self.capture is None:
            self.capture = cv2.VideoCapture(self.device)
            if not self.capture.isOpened():
                self.capture = None
                raise RuntimeError(f"no camera at {self.device!r}")
        ok, frame = self.capture.read()
        if not ok:
            raise RuntimeError(f"the camera at {self.device!r} gave no frame")
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        expected = (self.optics.pixels_y, self.optics.pixels_x)
        if grey.shape != expected:
            if self.resize:
                grey = cv2.resize(grey, (self.optics.pixels_x, self.optics.pixels_y),
                                  interpolation=cv2.INTER_LINEAR)
            else:
                raise RuntimeError(
                    f"the camera at {self.device!r} gives {grey.shape} frames, but the camera is "
                    f"{expected}; rescale the stream or fix the optics"
                )
        return grey


class TiledDevice:
    """One UVC device carrying several camera tiles side by side.

    The three-camera Pico exposes a single 480x120 stream: camera 1 in the
    left third, camera 2 in the middle, camera 3 on the right. One capture
    object is shared so the device is never opened three times, and a frame
    grabbed for one tile is reused by the other two within the same cycle.

    Drive it from tools/camera_stream.py with ``--device-tiled /dev/video0``.
    """

    def __init__(self, device, reuse_s=0.05):
        self.device = device
        self.reuse_s = float(reuse_s)
        self.capture = None
        self._frame = None
        self._grabbed_at = 0.0

    def open(self):
        """Open the device now; return False instead of raising when absent."""
        if self.capture is not None:
            return True
        capture = cv2.VideoCapture(self.device)
        if not capture.isOpened():
            capture.release()
            return False
        self.capture = capture
        return True

    def latest(self):
        now = time.monotonic()
        if self._frame is not None and (now - self._grabbed_at) <= self.reuse_s:
            return self._frame
        if self.capture is None:
            self.capture = cv2.VideoCapture(self.device)
            if not self.capture.isOpened():
                self.capture = None
                raise RuntimeError(f"no camera at {self.device!r}")
        ok, frame = self.capture.read()
        if not ok:
            raise RuntimeError(f"the camera at {self.device!r} gave no frame")
        self._frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self._grabbed_at = now
        return self._frame


class BlankSource:
    """A black frame, for a camera that is not there.

    Slicer must still get three images so the view binding never waits on a
    camera that will not appear; a missing device therefore streams black
    instead of failing the whole sender.
    """

    def __init__(self, optics):
        self.optics = optics

    def grab(self):
        return np.zeros((self.optics.pixels_y, self.optics.pixels_x), np.uint8)


class TiledSource:
    """One tile of a TiledDevice stream, shaped like any other FrameSource."""

    def __init__(self, device, tile, optics, tiles=3, resize=False):
        self.device = device
        self.tile = int(tile)
        self.tiles = int(tiles)
        self.optics = optics
        self.resize = bool(resize)

    def grab(self):
        grey = self.device.latest()
        tile_width = grey.shape[1] // self.tiles
        x0 = self.tile * tile_width
        tile = grey[:, x0:x0 + tile_width]
        expected = (self.optics.pixels_y, self.optics.pixels_x)
        if tile.shape != expected:
            if self.resize:
                tile = cv2.resize(
                    tile, (self.optics.pixels_x, self.optics.pixels_y),
                    interpolation=cv2.INTER_LINEAR)
            else:
                raise RuntimeError(
                    f"tile {self.tile} of {self.device.device!r} is {tile.shape}, "
                    f"but the camera is {expected}; pass --resize or fix the optics")
        return tile
