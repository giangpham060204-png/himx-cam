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
import math
import pathlib
import sys
from dataclasses import dataclass


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

import frame_sources  # noqa: E402
import synthetic_camera as cam  # noqa: E402

# A real detector on a real sensor does not do better than about this.
PIXEL_SIGMA_FLOOR = 0.1


LOGS_DIR = PROJECT_ROOT / "logs"


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

    def camera_positions(self):
        """Where the three cameras stand, for the current geometry."""
        angles = np.radians([90.0, 210.0, 330.0])
        radii = self.geometry.spread * np.array([1.0, 0.9, 1.1])
        heights = np.array([0.0, 60.0, 30.0])
        return [np.array([r * math.cos(a), r * math.sin(a), self.geometry.standoff + h])
                for r, a, h in zip(radii, angles, heights)]

    # -- seeing --------------------------------------------------------

    def render_all(self):
        """Draw one frame per camera. The only expensive step in the module."""
        self.frames = [camera.source.grab() for camera in self.cameras]
        return self.frames

    def _detect(self, index):
        """The corners and the centre of the marker in one camera, or None."""
        try:
            corners = cam.detect_marker(self.frames[index], cam.detector())
        except RuntimeError:
            return None
        ideal = cam.straight_lens(corners, self.optics)
        return ideal, cam.diagonal_crossing(ideal)

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

        seen, missing = [], []
        for index in range(len(self.cameras)):
            found = self._detect(index)
            if found is None:
                missing.append(self.cameras[index].name)
            else:
                seen.append(found[1])
        if missing:
            return self._remember(Outcome(
                status="not-readable",
                message=f"{', '.join(missing)} cannot read the marker",
            ))

        try:
            point = triangulate([camera.projection.tolist() for camera in self.cameras],
                                [tuple(float(c) for c in pixel) for pixel in seen])
        except ValueError as exc:
            return self._remember(Outcome(status="degenerate", message=str(exc)))

        point = np.asarray(point, float)
        ras = self.rig.to_ras(tuple(point))
        try:
            check_in_field_ras(dict(zip(("R", "A", "S"), ras)), self.frame)
        except OutOfFieldError as exc:
            return self._remember(Outcome(status="outside-field", message=str(exc)))

        residual = cam.reprojection_residual([camera.projection for camera in self.cameras],
                                             seen, point)
        truth_error = float(np.linalg.norm(point))
        return self._remember(Outcome(
            status="found",
            message="",
            pixels=tuple(tuple(float(c) for c in pixel) for pixel in seen),
            cube=ras_to_cube(ras, self.frame),
            ras=ras,
            residual_px=tuple(residual),
            error_mm=truth_error,
            predicted_mm=self._predicted(),
        ))

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
        )

    # -- helpers -------------------------------------------------------

    def _remember(self, outcome):
        self.outcome = outcome
        return outcome

    def _predicted(self):
        sigma = max(self.effective_sigma(), PIXEL_SIGMA_FLOOR)
        distance = math.sqrt(self.geometry.spread ** 2 + self.geometry.standoff ** 2)
        baseline = cam.pairwise_baseline(self.geometry.spread, self.geometry.standoff)
        return sigma * distance ** 2 / (self.optics.focal_length_pixels * baseline)

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
            return "click the object to nominate a target   (r render, p place, s save, q quit)"
        if outcome.status != "found":
            return f"{outcome.status}: {outcome.message}\n(r render, p place, s save, q quit)"
        cube = ", ".join(f"{value:.1f}" for value in outcome.cube)
        ras = ", ".join(f"{value:.1f}" for value in outcome.ras)
        residuals = " · ".join(f"({value:.2f})" for value in outcome.residual_px)
        text = (
            f"cube ({cube}) mm      RAS ({ras}) mm\n"
            f"error {outcome.error_mm:.2f} mm against truth      "
            f"predicted {outcome.predicted_mm:.2f} mm at sigma "
            f"{self.effective_sigma():.2f} px\n"
            f"field inside\n"
            f"seen c1 c2 c3      residuals {residuals}"
        )
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

    def __init__(self, sandbox, figure):
        self.sandbox = sandbox
        self.figure = figure
        self.panes = {}
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

        It also clears the standing nomination: the frames have changed, so a
        point found against the old ones is no longer the answer to this scene.
        """
        self.sandbox.render_all()
        self.sandbox.outcome = None
        self._paint()

    def _paint(self):
        """The frames, their overlays, and the readout.

        The window cannot call `figure_views` from `synthetic_camera.py` even
        though it draws the same four panes: that function builds its own figure,
        and this one needs axes it can attach clicks to. So the drawing lives
        here, and the numbers do not -- they come from the sandbox's own state.
        """
        for index, frame in enumerate(self.sandbox.frames):
            pane = self.panes[index]
            pane.clear()
            pane.imshow(frame, cmap="gray", vmin=0, vmax=255)
            pane.set_xticks([])
            pane.set_yticks([])
            markers = cam.marker_corners(self.sandbox.geometry.marker_mm)
            span = float(np.ptp(where_they_land(self.sandbox.cameras[index], markers), axis=0).max())
            pane.set_title(f"camera {index + 1}   marker spans {span:.0f} px", fontsize=9)
            if self.sandbox.outcome is not None and self.sandbox.outcome.status == "found":
                u_px, v_px = self.sandbox.outcome.pixels[index]
                pane.plot(u_px, v_px, marker="+", color="tab:red", ms=11, mew=1.4)
        self._paint_scene()
        self.figure.suptitle(self.sandbox.readout(), fontsize=10, y=0.02, va="bottom")
        self.figure.canvas.draw_idle()

    def _paint_scene(self):
        """The three frusta, the marker, the three rays, the true point and the one found."""
        pane = self.panes["scene"]
        pane.clear()
        markers = cam.marker_corners(self.sandbox.geometry.marker_mm)
        ring = np.vstack([markers, markers[:1]])
        pane.plot(ring[:, 0], ring[:, 1], ring[:, 2], color="tab:orange", lw=2,
                  label=f"printed square ({self.sandbox.geometry.marker_mm:.0f} mm)")
        pane.scatter(0, 0, 0, color="tab:green", marker="*", s=200, label="the true point")
        if self.sandbox.outcome is not None and self.sandbox.outcome.status == "found":
            pane.scatter(*self.sandbox.outcome.ras, color="tab:red", marker="x", s=90,
                         label="found from three rays")
            self._paint_rays(pane)
        positions = self.sandbox.camera_positions()
        for index, position in enumerate(positions):
            pane.scatter(*position, color="tab:blue", s=40, marker="s")
            pane.text(*position, f"  c{index + 1}", color="tab:blue", fontsize=8)
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
        for camera, pixel in zip(self.sandbox.cameras, outcome.pixels):
            origin, direction = ray_for(
                np.asarray(camera.projection, float).tolist(),
                (float(pixel[0]), float(pixel[1])))
            origin = np.asarray(origin, float)
            reach = float(np.linalg.norm(rig_point - origin))
            if not np.isfinite(reach) or reach <= 0.0:
                reach = float(self.sandbox.geometry.standoff)
            tip = origin + np.asarray(direction, float) * reach * 1.1
            pane.plot([origin[0], tip[0]], [origin[1], tip[1]], [origin[2], tip[2]],
                      color="tab:blue", lw=1.0, alpha=0.55)

    def on_press(self, event):
        pane = self._camera_at(event)
        if pane is None:
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
    parser.add_argument("--spread", type=float, default=150.0)
    parser.add_argument("--standoff", type=float, default=500.0)
    parser.add_argument("--aim", type=float, default=35.0)
    parser.add_argument("--marker-mm", type=float, default=40.0)
    parser.add_argument("--frames", type=int, default=4,
                        help="rendered frames per measurement in the report")
    parser.add_argument("--trials", type=int, default=300,
                        help="draws per cell of the accuracy table")
    args = parser.parse_args(argv)

    from targeting.frame import Frame
    optics = optics_for(args.optics)
    geometry = RigGeometry(spread=args.spread, standoff=args.standoff,
                           aim_offset=args.aim, marker_mm=args.marker_mm)
    box = Sandbox(optics=optics, geometry=geometry,
                  rig=load_rig(PROJECT_ROOT / "rig.json"),
                  frame=Frame.load(PROJECT_ROOT / "frame.json"))

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
    plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
