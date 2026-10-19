"""Compare a triangulated point with a distance a human measured with a tape.

This is the only check in the camera work that can catch a systematic error.
The residual cannot: three lines of sight can meet perfectly and still be wrong
together, if the intrinsics are off or the whole rig has moved. That failure is
silent, and this is what makes it speak.

    uv run tools/commissioning_check.py --simulate

Today the tool simulates the scene and checks the whole chain measures the known
distance back: render, detect, triangulate, compare. That proves the arithmetic
and the failure path before any hardware exists. The real-picture input is
deliberately absent: it needs each camera's pose, which on hardware comes from
calibration, and inventing poses here would produce a plausible wrong bias --
the one outcome a commissioning check must never produce.

What the tape measures: the distance from the marker's centre to the centroid of
the three camera origins, which the report prints in millimetres so the human can
put the tape where the software did. It is not the nominal standoff: the cameras
stand at three different heights, so the nominal number and the geometric
distance are different quantities.

A simulated run is expected to show a few tenths of a millimetre of bias from
the detect/triangulate chain itself; that number is not a definition error, and
revealing exactly that kind of systematic is what this tool exists for.
"""

import argparse
import pathlib
import sys
from dataclasses import dataclass


class Refused(Exception):
    """The run cannot be trusted: frames missing, uneven, or unreadable."""


def _resolve_project_root(_globals=None, _env=None):
    """Find the checkout root without scanning the disk (same rule as the sandbox)."""
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
        f"before exec(). __file__={here!r}"
    )


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import numpy as np  # noqa: E402

from targeting.frame import Frame  # noqa: E402
from targeting.optics import DEFAULT_MODULE, MODULES, optics_for  # noqa: E402
from targeting.rig import load_rig  # noqa: E402
from targeting.triangulation import ray_for, triangulate  # noqa: E402

import camera_sandbox  # noqa: E402
import synthetic_camera as cam  # noqa: E402


@dataclass(frozen=True)
class Summary:
    """What the runs add up to: a lean, a wobble, and the worst single instant."""

    runs: int
    bias_mm: float
    spread_mm: float
    worst_mm: float


def measure_frames(images, optics, projections):
    """Triangulate one set of frames: one picture per camera, in camera order."""
    if len(images) != len(projections):
        raise Refused(
            f"expected one frame per camera, got {len(images)} frames "
            f"for {len(projections)} cameras")
    pixels = []
    for index, (image, projection) in enumerate(zip(images, projections)):
        try:
            _, centre = cam.marker_centre(image, optics)
        except RuntimeError:
            raise Refused(f"camera {index + 1}: cannot read the marker") from None
        pixels.append((float(centre[0]), float(centre[1])))
    return np.asarray(triangulate([p.tolist() for p in projections], pixels), float), pixels


def summarise(points, rig_origin, measured_mm):
    """Bias against the tape, wobble across the runs, and the worst run."""
    origin = np.asarray(rig_origin, float)
    distances = np.asarray([float(np.linalg.norm(np.asarray(p, float) - origin))
                            for p in points], float)
    return Summary(runs=len(distances),
                   bias_mm=float(distances.mean() - measured_mm),
                   spread_mm=float(distances.std()),
                   worst_mm=float(np.max(np.abs(distances - measured_mm))))


def simulated_projections(standoff_mm, *, spread=150.0, aim=35.0, marker_mm=40.0,
                          optics=None):
    """The three projection matrices a sandbox at this geometry would use.

    Taken from the sandbox itself rather than assembled here, so a change to the
    rig's own layout cannot leave this tool measuring a different rig.
    """
    return [camera.projection for camera in simulated_scene(
        optics or optics_for(), standoff_mm, spread=spread, aim=aim,
        marker_mm=marker_mm).cameras]


def simulated_scene(optics, standoff_mm, *, spread=150.0, aim=35.0, marker_mm=40.0,
                    seed=5):
    """A sandbox whose scene this tool chose, so the true distance is known."""
    return camera_sandbox.Sandbox(
        optics=optics,
        geometry=camera_sandbox.RigGeometry(spread=spread, standoff=standoff_mm,
                                            aim_offset=aim, marker_mm=marker_mm),
        rig=load_rig(PROJECT_ROOT / "rig.json"),
        frame=Frame.load(PROJECT_ROOT / "frame.json"),
        seed=seed,
    )


def camera_origins(projections):
    """Where each camera stands, read out of its own projection matrix.

    The camera sits where the projection sends a point to zero, which is what
    ``ray_for`` solves. So the tape's reference point comes from the same
    matrices the triangulation inverts -- not from a second copy of the rig that
    could drift away from this one.
    """
    return np.asarray([ray_for(np.asarray(projection, float).tolist(), (0.0, 0.0))[0]
                       for projection in projections], float)


def scene_truth(origins, marker=None):
    """The distance a tape would show for a scene whose geometry we chose.

    The marker stands at the world origin in every scene this project draws, so
    the truth is the distance from that point to the centroid of the camera
    origins.
    """
    marker = np.zeros(3) if marker is None else np.asarray(marker, float)
    centroid = np.asarray(origins, float).mean(axis=0)
    return float(np.linalg.norm(marker - centroid))


def measure_all(shots, optics, projections):
    """One triangulated point per instant, in order."""
    return [measure_frames(images, optics, projections)[0] for images in shots]


def report(points, origins, measured_mm, tolerance_mm):
    """Print the run and return the exit code: 0 PASS, 1 FAIL."""
    centroid = np.asarray(origins, float).mean(axis=0)
    summary = summarise(points, centroid, measured_mm)
    print(f"cameras stand at a centroid of ({centroid[0]:.1f}, {centroid[1]:.1f},"
          f" {centroid[2]:.1f}) mm in the rig frame")
    print(f"tape value {measured_mm:.2f} mm, measured from that point to the marker")
    for number, point in enumerate(points, start=1):
        distance = float(np.linalg.norm(np.asarray(point, float) - centroid))
        print(f"  instant {number:03d}: ({point[0]:.2f}, {point[1]:.2f},"
              f" {point[2]:.2f}) mm, {distance:.2f} mm from the centroid")
    print(f"runs {summary.runs}   bias {summary.bias_mm:+.2f} mm   "
          f"spread {summary.spread_mm:.2f} mm   worst {summary.worst_mm:.2f} mm")
    ok = abs(summary.bias_mm) <= tolerance_mm and summary.worst_mm <= tolerance_mm
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    """Simulate a known distance and check the whole chain measures it back.

    There is deliberately no real-picture input yet. Reading a folder of
    photographs would need each camera's pose, which on hardware comes from
    calibration and does not exist in this repository; inventing poses here
    would produce a plausible wrong bias, which is the one outcome a
    commissioning check must never produce.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--simulate", action="store_true",
                        help="render a scene at a known distance and check the round trip")
    parser.add_argument("--instants", type=int, default=3,
                        help="how many measurements to take of the same scene")
    parser.add_argument("--standoff-mm", type=float, default=500.0,
                        help="how high the simulated rig stands above the marker")
    parser.add_argument("--spread-mm", type=float, default=150.0)
    parser.add_argument("--aim-mm", type=float, default=35.0)
    parser.add_argument("--marker-mm", type=float, default=40.0)
    parser.add_argument("--optics", default=DEFAULT_MODULE, choices=sorted(MODULES))
    parser.add_argument("--tolerance-mm", type=float, default=1.0,
                        help="how far the bias and the worst run may stray")
    parser.add_argument("--measured-mm", type=float, default=None,
                        help="accepted only to be refused: a simulated run knows the distance")
    args = parser.parse_args(argv)

    try:
        if not args.simulate:
            raise Refused("nothing to do: pass --simulate (real pictures need "
                          "calibrated camera poses, which do not exist yet)")
        if args.measured_mm is not None:
            raise Refused("a simulated run knows the distance; drop --measured-mm")
        if args.instants < 1:
            raise Refused("--instants must be at least 1")
        optics = optics_for(args.optics)
        box = simulated_scene(optics, args.standoff_mm, spread=args.spread_mm,
                              aim=args.aim_mm, marker_mm=args.marker_mm)
        projections = [camera.projection for camera in box.cameras]
        origins = camera_origins(projections)
        shots = [box.render_all() for _ in range(args.instants)]
        points = measure_all(shots, optics, projections)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:                      # rig or frame unreadable
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    return report(points, origins, scene_truth(origins), args.tolerance_mm)


if __name__ == "__main__":
    raise SystemExit(main())
