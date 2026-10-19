"""The code above the seam: name a target, solve for it, record it.

No window here, and no decision inside a callback anywhere below. Every step is
a plain function of plain values, so the whole flow can be tested without a
human clicking -- which is also what makes the window replaceable.

Run the window with:

    uv run tools/camera_sandbox.py

or write the figures and exit, without a window:

    uv run tools/camera_sandbox.py --out images

Command-line handling lives in main(); this module's importable surface is the
geometry and the two verbs, nominate() and place().
"""

import argparse
import cv2
import json
import math
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime


def _resolve_project_root(_globals=None, _env=None):
    """Find the checkout root without scanning the disk (same rule as make_field_cube)."""
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

import numpy as np  # noqa: E402

from targeting.converter import OutOfFieldError, check_in_field_ras, ras_to_cube  # noqa: E402
from targeting.optics import DEFAULT_MODULE, MODULES, optics_for  # noqa: E402
from targeting.rig import load_rig  # noqa: E402
from targeting.triangulation import (camera_pose, project as project_point,  # noqa: E402
                                    projection_matrix, ray_for, triangulate)
from targeting.camera_log import append_camera_target  # noqa: E402
from targeting.probe import PROBE_FILE, read_probe  # noqa: E402
from targeting.clicks import CLICKS_FILE, read_clicks  # noqa: E402
from targeting.logbook import newest_target  # noqa: E402

import frame_sources  # noqa: E402
import synthetic_camera as cam  # noqa: E402

# A real detector on a real sensor does not do better than about this.
PIXEL_SIGMA_FLOOR = 0.1

# What a hand-placed pixel is worth. A careful operator clicking a marker tens of
# pixels across lands within a few pixels; quoting the detector's 0.3 px here
# would claim a precision the measurement does not have, and pretending
# otherwise is the one thing this mode exists to make visible.
CLICK_SIGMA_PX = 5.0


LOGS_DIR = PROJECT_ROOT / "logs"
CAMERA_FRAMES_DIR = LOGS_DIR / "camera-frames"

# Colour says which camera; shape says what the thing is. The three colours are
# also the three marks a reader can name out loud, so nothing else may use them.
CAMERA_COLOURS = ("tab:red", "tab:green", "tab:orange")


@dataclass(frozen=True)
class RigGeometry:
    """The rig as four numbers, because that is how the accuracy law reads."""

    spread: float = 150.0          # how far the cameras sit off-axis, mm
    standoff: float = 500.0        # how high above the object, mm
    aim_offset: float = 35.0       # how far the rig points from the object, mm
    marker_mm: float = 40.0        # side of the printed ArUco square


@dataclass
class Outcome:
    """What a nomination produced, or why it produced nothing."""

    status: str                     # found | not-on-object | not-readable
                                    # | degenerate | outside-field
    message: str = ""
    pixels: tuple = ()
    cube: tuple = ()
    ras: tuple = ()
    residual_px: tuple = ()
    error_mm: float = None          # against the truth; None when nothing was found
    predicted_mm: float = None      # what a given pixel sigma would give
    sigma_px: float = None          # the sigma that prediction used
    found_by: str = "detected"      # detected | manual


@dataclass(frozen=True)
class Repeat:
    """How much the same object moved while it was measured `runs` times over."""

    runs: int
    found: int
    failed: int
    mean: tuple = None
    std_mm: tuple = None
    max_mm: float = None


class Sandbox:
    """The rig, its cameras, the scene, and the two verbs.

    Rendering is on demand. A click costs no re-render: the frames from the last
    render are reused, which is why the click path feels immediate.
    """

    def __init__(self, optics, geometry, rig, frame, seed=7, sigma=None, sources=None):
        self.optics = optics
        self.geometry = geometry
        self.rig = rig
        self.frame = frame
        self.sigma = sigma
        self.sources = list(sources) if sources else None
        self.rng = np.random.default_rng(seed)
        self.frames = []
        self.outcome = None
        self.repeat_result = None
        self.hypothesis_cube = None
        self.hypothesis_ras = None
        self.hypothesis_note = ""
        self.applied_clicks = None
        self.rebuild()

    # -- geometry ------------------------------------------------------

    def rebuild(self):
        """Place the three cameras for the current geometry."""
        self.cameras = []
        for index, position in enumerate(self.camera_positions()):
            position = np.asarray(position, float)
            aim = np.array([self.geometry.aim_offset, self.geometry.aim_offset,
                            position[2] - self.geometry.standoff])
            R, t = camera_pose(tuple(position), tuple(aim))
            self.cameras.append(frame_sources.Camera(
                name=f"c{index + 1}",
                optics=self.optics,
                projection=np.array(projection_matrix(cam.camera_matrix(self.optics), R, t)),
                source=(self.sources[index] if self.sources else
                        frame_sources.SyntheticSource(self.optics, (np.array(R), np.array(t)),
                                                      self.rng,
                                                      marker_mm=self.geometry.marker_mm)),
            ))
        self.frames = []
        self.outcome = None
        self.repeat_result = None
        self.applied_clicks = None

    def camera_positions(self):
        """Where the three cameras stand, for the current geometry."""
        angles = np.radians([90.0, 210.0, 330.0])
        radii = self.geometry.spread * np.array([1.0, 0.9, 1.1])
        heights = np.array([0.0, 60.0, 30.0])
        return [np.array([r * math.cos(a), r * math.sin(a), self.geometry.standoff + h])
                for r, a, h in zip(radii, angles, heights)]

    @property
    def simulated(self):
        """Did this sandbox place the marker, or was it handed its pictures?

        Only when the scene was generated here does the code know where the
        marker is. With recorded or live frames there is no truth to compare
        against, and reporting one anyway would be a plausible wrong number --
        the worst outcome this project recognises.
        """
        return self.sources is None

    # -- seeing --------------------------------------------------------

    def render_all(self):
        """Ask every source for its next frame. The only expensive step here.

        In the sandbox that is a render; on a rig it is a capture. Nothing above
        this line may care which.
        """
        self.frames = [camera.source.grab() for camera in self.cameras]
        return self.frames

    def _detect(self, index):
        """The corners and the centre of the marker in one camera, or None."""
        try:
            return cam.marker_centre(self.frames[index], self.optics)
        except RuntimeError:
            return None

    # -- the verbs -----------------------------------------------------

    def nominate(self, camera_index, u_px, v_px):
        """Nominate the object by clicking it in one camera.

        The click only says *which* object. The measurement is detection's job,
        in every camera, exactly as the real system will work.
        """
        if not self.frames:
            self.render_all()

        here = self._detect(camera_index)
        if here is None:
            return self._remember(Outcome(
                status="not-readable",
                message=f"{self.cameras[camera_index].name} cannot read the marker",
            ))
        quad, _ = here
        if not _inside(np.asarray([u_px, v_px]), quad):
            return self._remember(Outcome(
                status="not-on-object", message="click the object",
            ))
        self.repeat_result = None
        return self._remember(self._measure())

    def nominate_manually(self, pixels):
        """Solve for a point from three hand-placed pixels, one per camera.

        No detector is involved: whatever the operator clicked *is* the
        measurement. That is why the predicted error quotes the click sigma
        rather than the detector's -- three hand-placed lines of sight do not
        meet, and the residual says by how much.
        """
        pixels = tuple(tuple(float(value) for value in pixel) for pixel in pixels)
        if len(pixels) != len(self.cameras):
            raise ValueError(
                f"a manual nomination needs one pixel from each of the "
                f"{len(self.cameras)} cameras, got {len(pixels)}")
        try:
            point = triangulate([camera.projection.tolist() for camera in self.cameras],
                                pixels)
        except ValueError as exc:
            return self._remember(Outcome(status="degenerate", message=str(exc)))
        self.repeat_result = None
        return self._remember(self._finish(point, pixels, found_by="manual",
                                          sigma=CLICK_SIGMA_PX))

    def _measure(self):
        """Detect the marker in every camera and solve for the point.

        The click says which object; this turns pictures into numbers. It is the
        same code path whether it runs once, for a click, or `times` times over,
        for a repeat, so a repeat cannot quietly measure something else.
        """
        seen, missing = [], []
        for index in range(len(self.cameras)):
            found = self._detect(index)
            if found is None:
                missing.append(self.cameras[index].name)
            else:
                seen.append(found[1])
        if missing:
            return Outcome(status="not-readable",
                           message=f"{', '.join(missing)} cannot read the marker")
        try:
            point = triangulate([camera.projection.tolist() for camera in self.cameras],
                                [tuple(float(c) for c in pixel) for pixel in seen])
        except ValueError as exc:
            return Outcome(status="degenerate", message=str(exc))
        return self._finish(point, seen, found_by="detected", sigma=self.effective_sigma())

    def _finish(self, point, pixels, *, found_by, sigma):
        """Turn a solved point into an Outcome, or into the reason it is refused."""
        point = np.asarray(point, float)
        ras = self.rig.to_ras(tuple(point))
        try:
            check_in_field_ras(dict(zip(("R", "A", "S"), ras)), self.frame)
        except OutOfFieldError as exc:
            return Outcome(status="outside-field", message=str(exc))
        return Outcome(
            status="found",
            message="",
            pixels=tuple(tuple(float(c) for c in pixel) for pixel in pixels),
            cube=ras_to_cube(ras, self.frame),
            ras=ras,
            residual_px=tuple(cam.reprojection_residual(
                [camera.projection for camera in self.cameras], pixels, point)),
            error_mm=(float(np.linalg.norm(point)) if self.simulated else None),
            predicted_mm=self._predicted(sigma),
            sigma_px=float(sigma),
            found_by=found_by,
        )

    def place(self, logs_dir=None):
        """Write the accepted point to the camera log. Returns the file."""
        if self.outcome is None:
            raise RuntimeError("nominate before placing")
        if self.outcome.status != "found":
            raise RuntimeError(f"nothing to place: {self.outcome.message}")
        return append_camera_target(
            LOGS_DIR if logs_dir is None else pathlib.Path(logs_dir),
            cube=self.outcome.cube,
            ras=self.outcome.ras,
            residual_px=float(np.mean(self.outcome.residual_px)),
            pixels=self.outcome.pixels,
            found_by=self.outcome.found_by,
        )

    # -- helpers -------------------------------------------------------

    def _remember(self, outcome):
        self.outcome = outcome
        return outcome

    def repeat(self, times=8):
        """Measure the same object `times` times over and report how much it moves.

        This is repeatability, not accuracy: nothing here knows where the object
        really is. Each run asks every source for a fresh frame -- a real camera
        never repeats itself, and neither may the renderer, or this would measure
        nothing at all. On hardware the spread also carries whatever moved
        between the grabs, which is the point of having it.
        """
        if times < 2:
            raise ValueError("a repeat needs at least two runs")
        cubes, failed = [], 0
        for _ in range(times):
            self.render_all()
            outcome = self._measure()
            if outcome.status == "found":
                cubes.append(outcome.cube)
            else:
                failed += 1
        self.outcome = None          # found against frames that no longer exist
        if cubes:
            stacked = np.asarray(cubes, float)
            mean = stacked.mean(axis=0)
            self.repeat_result = Repeat(
                runs=times, found=len(cubes), failed=failed,
                mean=tuple(float(value) for value in mean),
                std_mm=tuple(float(value) for value in stacked.std(axis=0)),
                max_mm=float(np.max(np.linalg.norm(stacked - mean, axis=1))),
            )
        else:
            self.repeat_result = Repeat(runs=times, found=0, failed=failed)
        return self.repeat_result

    def load_hypothesis(self, logs_dir=None):
        """Load the newest point a human placed in Slicer, to be checked.

        A hypothesis is not a measurement and is never logged: the camera log
        holds what the cameras saw, and letting a guess in would cost it the
        only property worth having.
        """
        target, note = newest_target(
            LOGS_DIR if logs_dir is None else pathlib.Path(logs_dir))
        self.hypothesis_note = note
        if target is None:
            self.hypothesis_cube = None
            self.hypothesis_ras = None
            return note
        self.hypothesis_cube = tuple(float(value) for value in target.cube)
        self.hypothesis_ras = tuple(float(value) for value in target.ras)
        return note

    def clear_hypothesis(self):
        """Forget the loaded hypothesis and the note that came with it."""
        self.hypothesis_cube = None
        self.hypothesis_ras = None
        self.hypothesis_note = ""

    def follow_clicks(self, logs_dir=None):
        """Solve the click set Slicer published, once per new set.

        A click set is a direct measurement of the pictures, so it wins over
        the probe: the window tries this first. An unchanged set solves
        nothing twice; a missing or malformed file is reported, never solved.
        Returns (applied, note).
        """
        folder = LOGS_DIR if logs_dir is None else pathlib.Path(logs_dir)
        clicks, note = read_clicks(folder / CLICKS_FILE)
        if clicks is None:
            return False, note
        pixels = tuple(tuple(float(value) for value in pair) for pair in clicks.pixels)
        if self.applied_clicks is not None and tuple(self.applied_clicks) == pixels:
            return False, f"{CLICKS_FILE}: clicks unchanged ({note})"
        self.applied_clicks = pixels
        self.nominate_manually(pixels)
        return True, f"{CLICKS_FILE}: clicks solved ({note})"

    def follow_probe(self, logs_dir=None):
        """Take the point Slicer is publishing right now, if it has one.

        The same hypothesis the `l` key loads from the click log, only live: the
        operator moves a probe in Slicer and the lines of sight move with it. It
        is still a human's point and never a measurement -- nothing on this path
        touches the camera log.

        A probe file that has gone away leaves the last point standing: closing
        Slicer should not erase the reference the operator was comparing
        against. The note says what happened either way.
        """
        folder = LOGS_DIR if logs_dir is None else pathlib.Path(logs_dir)
        probe, note = read_probe(folder / PROBE_FILE)
        self.hypothesis_note = note
        if probe is None:
            return note
        self.hypothesis_cube = tuple(float(value) for value in probe.cube)
        self.hypothesis_ras = tuple(float(value) for value in probe.ras)
        self.hypothesis_note = f"live probe from Slicer ({note})"
        return self.hypothesis_note

    @property
    def hypothesis_point(self):
        """The hypothesis in the rig frame, where the projections live."""
        if self.hypothesis_ras is None:
            return None
        return np.asarray(self.rig.to_rig(self.hypothesis_ras), float)

    def _predicted(self, sigma=None):
        """What a pixel sigma this size would cost at this rig's geometry."""
        sigma = max(self.effective_sigma() if sigma is None else float(sigma),
                    PIXEL_SIGMA_FLOOR)
        distance = math.sqrt(self.geometry.spread ** 2 + self.geometry.standoff ** 2)
        baseline = cam.pairwise_baseline(self.geometry.spread, self.geometry.standoff)
        return sigma * distance ** 2 / (self.optics.focal_length_pixels * baseline)

    def _repeat_tail(self):
        """The repeat line when there is one, else nothing at all."""
        if self.repeat_result is None:
            return ""
        return "\n" + self._repeat_line()

    def _repeat_line(self):
        """The last repeat as one line, or why there is nothing to report."""
        result = self.repeat_result
        if result.found == 0:
            return (f"repeat {result.runs}: nothing found in any run "
                    f"({result.failed} failed) -- check the marker and the lighting")
        spread = ", ".join(f"{value:.2f}" for value in result.std_mm)
        line = (f"repeat {result.runs}: spread std ({spread}) mm, "
                f"worst {result.max_mm:.2f} mm -- repeatability, not accuracy")
        if result.failed:
            line += f", {result.failed} failed"
        if result.max_mm == 0.0:
            # A renderer asked for the same scene twice returns pictures whose
            # noise differs and whose detected corners do not: OpenCV's corner
            # estimate is bit-identical across those grabs. A zero spread is
            # therefore true and uninformative, and reading it as "the detector
            # is perfect" would be the wrong lesson.
            line += ("  (an unchanged synthetic scene repeats exactly, so this "
                     "says nothing about the detector; on hardware the spread "
                     "will be larger)")
        return line

    def _hypothesis_tail(self):
        """The loaded Slicer point, and how far the measurement sits from it."""
        if self.hypothesis_cube is not None:
            cube = ", ".join(f"{value:.1f}" for value in self.hypothesis_cube)
            text = (f"\nhypothesis from Slicer: cube ({cube}) mm"
                    f" -- a human's point, not a measurement")
            if self.outcome is not None and self.outcome.status == "found":
                apart = float(np.linalg.norm(
                    np.asarray(self.outcome.cube, float)
                    - np.asarray(self.hypothesis_cube, float)))
                text += f"\nmeasured point is {apart:.1f} mm from the hypothesis"
            return text
        elif self.hypothesis_note:
            return f"\nno hypothesis: {self.hypothesis_note}"
        return ""

    def effective_sigma(self):
        """The detector's scatter, measured once on rendered frames."""
        if self.sigma is None:
            measured = cam.measure_detector(self.optics, self.geometry.spread,
                                            self.geometry.standoff, np.random.default_rng(2),
                                            frames=4)
            self.sigma = float(np.hypot(*measured["scatter"]))
        return self.sigma

    def readout(self):
        """One block of text with every number the operator needs, and why not.

        Kept as a string so it can be tested without a window, and drawn by the
        window without a second copy of the arithmetic.
        """
        outcome = self.outcome
        if outcome is None:
            hint = ("click the object to nominate a target   "
                    "(m manual, n repeat, l slicer, c clear, "
                    "r render, p place, s save, q quit)")
            return hint + self._repeat_tail() + self._hypothesis_tail()
        if outcome.status != "found":
            return (f"{outcome.status}: {outcome.message}\n"
                    "(m manual, n repeat, l slicer, c clear, "
                    "r render, p place, s save, q quit)") + self._repeat_tail() + self._hypothesis_tail()
        cube = ", ".join(f"{value:.1f}" for value in outcome.cube)
        ras = ", ".join(f"{value:.1f}" for value in outcome.ras)
        residuals = " · ".join(f"({value:.2f})" for value in outcome.residual_px)
        sigma = float(outcome.sigma_px if outcome.sigma_px is not None
                      else self.effective_sigma())
        truth = ("truth unknown (frames were given, not rendered)"
                 if outcome.error_mm is None
                 else f"error {outcome.error_mm:.2f} mm against truth (simulated)")
        text = (
            f"cube ({cube}) mm      RAS ({ras}) mm\n"
            f"{truth}      predicted {outcome.predicted_mm:.2f} mm at sigma {sigma:.2f} px\n"
            f"field inside\n"
            f"seen c1 c2 c3 ({outcome.found_by})      residuals {residuals}"
        )
        text += self._repeat_tail()
        text += self._hypothesis_tail()
        # A high residual is sometimes real, so the point stays placeable --
        # but on real hardware there is no truth line, and this is the only
        # quality signal the operator has.
        worst = max(outcome.residual_px) if outcome.residual_px else 0.0
        if worst > 2.0:
            text += (f"\nwarning: residual {worst:.1f} px is high "
                     f"-- check the marker and the lighting")
        return text


def where_it_lands(camera, point):
    """Where one world point lands in one camera, through the solver's projector.

    The matrix form of the projection lives in targeting/triangulation.py -- the
    pure code that will invert it -- so this asks the same question the solver
    asks, and there is one definition of what a projection matrix means.
    """
    plain = np.asarray(camera.projection, float).tolist()
    flat = np.asarray(point, float).reshape(-1)
    return np.array(project_point(plain, tuple(float(value) for value in flat)))


def where_they_land(camera, points):
    """Where several world points land in one camera."""
    return np.array([where_it_lands(camera, point)
                     for point in np.asarray(points, float).reshape(-1, 3)])


def _inside(point, quad):
    """Is this pixel inside the marker's quadrilateral? (ray casting)"""
    inside = False
    count = len(quad)
    for index in range(count):
        a, b = np.asarray(quad[index], float), np.asarray(quad[(index + 1) % count], float)
        if (a[1] > point[1]) != (b[1] > point[1]):
            crossing = (b[0] - a[0]) * (point[1] - a[1]) / (b[1] - a[1]) + a[0]
            if point[0] < crossing:
                inside = not inside
    return inside


class SandboxWindow:
    """The four panes, and the wiring that turns events into nominations.

    It holds no decisions of its own: every question is answered by Sandbox, so
    this class can be replaced, or driven by a test, without changing an answer.
    """

    def __init__(self, sandbox, figure, logs_dir=None):
        self.sandbox = sandbox
        self.figure = figure
        self.logs_dir = logs_dir
        self.panes = {}
        self.manual = False
        self.clicked = {}          # camera index -> (u, v), while three are gathered
        self.following = False
        self.timer = None
        self.figure.canvas.mpl_connect("button_press_event", self.on_press)
        self.figure.canvas.mpl_connect("motion_notify_event", self.on_motion)
        self.figure.canvas.mpl_connect("key_press_event", self.on_key)

    def draw(self):
        """Lay out the panes. Called once, before the first render."""
        self.figure.clear()
        grid = self.figure.add_gridspec(1, 4, width_ratios=[1, 1, 1, 1.5], wspace=0.25)
        self.panes = {"grid": grid}
        for index in range(3):
            self.panes[index] = self.figure.add_subplot(grid[0, index])
            self.panes[index].set_xticks([])
            self.panes[index].set_yticks([])
        self.panes["scene"] = self.figure.add_subplot(grid[0, 3], projection="3d")
        self.figure.canvas.draw()

    def render(self):
        """Redraw the three frames and the scene. The expensive step.

        It also clears the standing nomination and any half-finished manual
        clicks: the frames have changed, so a point found against the old ones
        is no longer the answer to this scene.
        """
        self.sandbox.render_all()
        self.sandbox.outcome = None
        self.clicked = {}
        self._paint()

    def _paint(self):
        """The frames, their overlays, and the readout.

        The window cannot call `figure_views` from `synthetic_camera.py` even
        though it draws the same four panes: that function builds its own figure,
        and this one needs axes it can attach clicks to. So the drawing lives
        here, and the numbers do not -- they come from the sandbox's own state.

        Each pane draws what the detector saw, what the accepted point predicts,
        and the line between them. That line is the residual in pixels, and it is
        drawn on the picture rather than quoted in a table so that a wrong answer
        is visible without reading a number.
        """
        outcome = self.sandbox.outcome
        found = outcome is not None and outcome.status == "found"
        world = None
        if found:
            world = np.asarray(
                self.sandbox.rig.to_rig(tuple(float(v) for v in outcome.ras)), float)
        for index, frame in enumerate(self.sandbox.frames):
            pane = self.panes[index]
            pane.clear()
            pane.imshow(frame, cmap="gray", vmin=0, vmax=255)
            pane.set_xticks([])
            pane.set_yticks([])
            markers = cam.marker_corners(self.sandbox.geometry.marker_mm)
            span = float(np.ptp(where_they_land(self.sandbox.cameras[index], markers), axis=0).max())
            title = f"camera {index + 1}   marker spans {span:.0f} px"
            if found:
                seen = np.asarray(outcome.pixels[index], float)
                predicted = where_it_lands(self.sandbox.cameras[index], world)
                title += f"   residual {float(outcome.residual_px[index]):.2f} px"
                pane.plot(seen[0], seen[1], marker="+", color=CAMERA_COLOURS[index],
                          ms=11, mew=1.4)
                pane.plot(predicted[0], predicted[1], marker="o", color=CAMERA_COLOURS[index],
                          ms=9, mew=1.4, mfc="none")
                pane.plot([seen[0], predicted[0]], [seen[1], predicted[1]],
                          color=CAMERA_COLOURS[index], lw=1.0, alpha=0.8)
            if self.sandbox.hypothesis_point is not None:
                where = where_it_lands(self.sandbox.cameras[index],
                                       self.sandbox.hypothesis_point)
                pane.plot(where[0], where[1], marker="D", color=CAMERA_COLOURS[index],
                          ms=7, mfc="none", mew=1.3)
            if index in self.clicked:
                u_px, v_px = self.clicked[index]
                pane.plot(u_px, v_px, marker="o", color=CAMERA_COLOURS[index], ms=7)
            pane.set_title(title, fontsize=9, color=CAMERA_COLOURS[index])
        self._paint_scene()
        text = self.sandbox.readout()
        if self.manual and self.clicked:
            text += (f"\nmanual: {len(self.clicked)} of "
                     f"{len(self.sandbox.cameras)} cameras clicked")
        self.figure.suptitle(text, fontsize=10, y=0.02, va="bottom")
        self.figure.canvas.draw_idle()

    def _paint_scene(self):
        """The three frusta, the marker, the three rays, the true point and the one found."""
        pane = self.panes["scene"]
        pane.clear()
        markers = cam.marker_corners(self.sandbox.geometry.marker_mm)
        ring = np.vstack([markers, markers[:1]])
        pane.plot(ring[:, 0], ring[:, 1], ring[:, 2], color="grey", lw=2, ls="--",
                  label=f"printed square ({self.sandbox.geometry.marker_mm:.0f} mm)")
        if self.sandbox.outcome is not None and self.sandbox.outcome.error_mm is not None:
            pane.scatter(0, 0, 0, color="tab:blue", marker="*", s=200,
                         label="the true point (simulated)")
        if self.sandbox.outcome is not None and self.sandbox.outcome.status == "found":
            point = self.sandbox.outcome.ras
            pane.scatter(*point, color="white", marker="x", s=170, lw=3)
            pane.scatter(*point, color="black", marker="x", s=90, lw=1.6,
                         label="found from three rays")
            self._paint_rays(pane)
        if self.sandbox.hypothesis_cube is not None:
            here = np.asarray(self.sandbox.hypothesis_ras, float)
            pane.scatter(*here, color="tab:purple", marker="D", s=70,
                         label="a point placed in Slicer")
        for index, position in enumerate(self.sandbox.camera_positions()):
            pane.scatter(*position, color=CAMERA_COLOURS[index], s=40, marker="s")
            pane.text(*position, f"  c{index + 1}", color=CAMERA_COLOURS[index], fontsize=8)
        pane.set_xlabel("R (mm)")
        pane.set_ylabel("A (mm)")
        pane.set_zlabel("S (mm)")
        pane.legend(fontsize=7, loc="upper left")

    def _paint_rays(self, pane):
        """One line of sight per camera, from the pure ray each pixel saw.

        The origin and direction come from ``ray_for`` applied to that
        camera's own projection matrix and measured pixel, so nothing here
        reaches below the frame-source seam for a pose. Each ray is drawn a
        little past the found point, in rig coordinates -- the same frame the
        camera glyphs are drawn in.
        """
        outcome = self.sandbox.outcome
        rig_point = np.asarray(
            self.sandbox.rig.to_rig(tuple(float(v) for v in outcome.ras)), float)
        for index, (camera, pixel) in enumerate(zip(self.sandbox.cameras, outcome.pixels)):
            origin, direction = ray_for(
                np.asarray(camera.projection, float).tolist(),
                (float(pixel[0]), float(pixel[1])))
            origin = np.asarray(origin, float)
            reach = float(np.linalg.norm(rig_point - origin))
            if not np.isfinite(reach) or reach <= 0.0:
                reach = float(self.sandbox.geometry.standoff)
            tip = origin + np.asarray(direction, float) * reach * 1.1
            pane.plot([origin[0], tip[0]], [origin[1], tip[1]], [origin[2], tip[2]],
                      color=CAMERA_COLOURS[index], lw=1.0, alpha=0.55)

    def follow(self, logs_dir=None):
        """Start or stop following the probe Slicer publishes.

        The timer, not the key press, is what makes this live: every half second
        the published point is read, and a point that moved is redrawn. A point
        that did not move costs one file read and no repaint.
        """
        if self.following:
            self.following = False
            if self.timer is not None:
                self.timer.stop()
            self._paint()
            return
        self.following = True
        if self.logs_dir is None and logs_dir is not None:
            self.logs_dir = logs_dir
        if self.timer is None:
            self.timer = self.figure.canvas.new_timer(interval=500)
            self.timer.add_callback(self._follow_tick)
        self.timer.start()
        self._follow_tick()

    def _follow_tick(self):
        """Clicks first, the probe only when there is no click set."""
        before = (self.sandbox.hypothesis_cube, self.sandbox.outcome)
        applied, _note = self.sandbox.follow_clicks(self.logs_dir)
        if not applied:
            self.sandbox.follow_probe(self.logs_dir)
        if (self.sandbox.hypothesis_cube, self.sandbox.outcome) != before:
            self._paint()

    def on_press(self, event):
        pane = self._camera_at(event)
        if pane is None:
            return
        if self.manual:
            self.clicked[pane] = (event.xdata, event.ydata)
            if len(self.clicked) == len(self.sandbox.cameras):
                pixels = [self.clicked[index]
                          for index in range(len(self.sandbox.cameras))]
                self.clicked = {}
                self.sandbox.nominate_manually(pixels)
            self._paint()
            return
        self.sandbox.nominate(pane, event.xdata, event.ydata)
        self._paint()

    def on_motion(self, event):
        """A drag is not a second click."""
        return None

    def on_key(self, event):
        if event.key == "r":
            self.render()
        elif event.key == "p":
            self.place()
        elif event.key == "n":
            self.sandbox.repeat()
            self._paint()
        elif event.key == "l":
            self.sandbox.load_hypothesis()
            self._paint()
        elif event.key == "L":
            self.follow()
        elif event.key == "c":
            self.sandbox.clear_hypothesis()
            self.clicked = {}
            self._paint()
        elif event.key == "m":
            self.manual = not self.manual
            self.clicked = {}
            self._paint()
        elif event.key == "s":
            self.save("camera-sandbox.png")

    def _camera_at(self, event):
        if event.inaxes is None:
            return None
        for index in range(3):
            if event.inaxes is self.panes.get(index):
                return index
        return None

    def place(self, logs_dir=None):
        """Write the accepted point, or repaint the hint when there is none.

        Pressing `p` with nothing nominated raises in Sandbox; the operator
        should get guidance (the readout's "click the object" hint), not a
        console traceback.
        """
        try:
            return self.sandbox.place(logs_dir)
        except RuntimeError:
            self._paint()
            return None

    def save(self, path):
        self.figure.savefig(path, dpi=110, bbox_inches="tight")
        return path


def main(argv=None):
    """Open the window, or with --out write the figures and exit."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--optics", default=DEFAULT_MODULE, choices=sorted(MODULES))
    parser.add_argument("--out", default=None,
                        help="write figures here and exit, without a window")
    parser.add_argument("--write-frames", default=None,
                        help="write one frame per camera here, with a manifest, and exit")
    parser.add_argument("--spread", type=float, default=150.0)
    parser.add_argument("--standoff", type=float, default=500.0)
    parser.add_argument("--aim", type=float, default=35.0)
    parser.add_argument("--marker-mm", type=float, default=40.0)
    parser.add_argument("--frames", type=int, default=4,
                        help="rendered frames per measurement in the report")
    parser.add_argument("--trials", type=int, default=300,
                        help="draws per cell of the accuracy table")
    parser.add_argument("--follow", action="store_true",
                        help="start by following the probe Slicer publishes")
    args = parser.parse_args(argv)

    from targeting.frame import Frame
    optics = optics_for(args.optics)
    geometry = RigGeometry(spread=args.spread, standoff=args.standoff,
                           aim_offset=args.aim, marker_mm=args.marker_mm)
    box = Sandbox(optics=optics, geometry=geometry,
                  rig=load_rig(PROJECT_ROOT / "rig.json"),
                  frame=Frame.load(PROJECT_ROOT / "frame.json"))

    if args.write_frames:
        out = pathlib.Path(args.write_frames)
        out.mkdir(parents=True, exist_ok=True)
        cameras = []
        for index, frame in enumerate(box.render_all(), start=1):
            name = f"camera-{index}.png"
            cv2.imwrite(str(out / name), frame)
            cameras.append({"name": f"c{index}", "file": name,
                            "width": int(frame.shape[1]), "height": int(frame.shape[0])})
        manifest = {"optics": args.optics,
                    "when": datetime.now().isoformat(timespec="seconds"),
                    "cameras": cameras}
        (out / "camera-frames.json").write_text(json.dumps(manifest, indent=2),
                                                encoding="utf-8")
        print(f"wrote {len(cameras)} frames and camera-frames.json to {out}")
        return 0

    if args.out:
        import matplotlib
        matplotlib.use("Agg")
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        report = cam.build_report(optics, marker_mm=args.marker_mm,
                                  spread=args.spread, standoff=args.standoff,
                                  frames=args.frames, trials=args.trials,
                                  aim=args.aim)
        cam.figure_views(report, out / "camera-views.png")
        cam.figure_accuracy(report, out / "camera-accuracy.png")
        print(cam.format_report(report))
        print(f"\nwrote {out / 'camera-views.png'}")
        print(f"wrote {out / 'camera-accuracy.png'}")
        return 0

    import matplotlib
    import matplotlib.pyplot as plt
    if matplotlib.get_backend().lower().startswith("agg"):
        print("no window available: this needs a screen. Use --out DIR to write "
              "the figures instead.", file=sys.stderr)
        return 1
    figure = plt.figure(figsize=(15, 5.5))
    window = SandboxWindow(box, figure)
    window.draw()
    window.render()
    if args.follow:
        window.follow()
    plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
