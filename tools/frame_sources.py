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


class CameraSource:
    """A real camera. Written now so the shape is fixed; inert without hardware."""

    def __init__(self, device, optics):
        self.device = device
        self.optics = optics
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
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
