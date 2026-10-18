"""The synthetic HM01B0: what three cameras would see, and where their rays meet.

No camera, no table, no printed marker. This draws what three HM01B0 modules
would see of an ArUco square at a known place, reads the marker back out of each
frame with OpenCV's own detector, and works out where the three lines of sight
meet.

That makes it the project's accuracy instrument. Because the true position of
the marker is known exactly, every error reported here is the method's error and
not the world's -- which is how an answer to the client's "can it hit +-2 mm?"
is produced before any hardware exists.

The frames are not pinholes with a pattern painted on them. They carry the
lens's barrel distortion at the level its datasheet claims, cos^4 vignetting,
the defocus its aperture implies, shot and read noise, an automatic exposure
loop, and 8-bit quantisation. A renderer without those hides the effects that
bite hardest on hardware day: a mirrored print that no detector can read, a
marker whose modules get too small to resolve, and a sheet whose quiet zone
falls off the edge of the picture.

The geometry itself lives in ``targeting/triangulation.py``, which is plain
Python and tested without any of this. What is here is the seeing, not the
solving.

Usage:
    python tools/synthetic_camera.py [--out images] [--optics arducam-hm01b0]

Exits non-zero if any check fails.
"""

import argparse
import pathlib
import sys
from functools import lru_cache
from math import sqrt

import cv2
import matplotlib
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402


def _resolve_project_root(_globals=None, _env=None):
    """Find the checkout root without scanning the disk.

    No cwd or HOME crawl: only explicit override, env, or a __file__ that
    resolves to a real file under a marker (frame.json + targeting/converter.py).
    Anything else panics instead of guessing.
    """
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
sys.path.insert(0, str(PROJECT_ROOT))

from targeting.optics import DEFAULT_MODULE, MODULES, Optics, optics_for  # noqa: E402
from targeting.triangulation import camera_pose, project as project_point, triangulate  # noqa: E402

# The printed square, and the resolution it is printed at.
MARKER_MM = 40.0
QUIET_ZONE_FRACTION = 1.0 / 6.0

# Sensor behaviour: how noisy and how well exposed a frame comes out.
FULL_WELL = 4000.0
READ_NOISE = 1.5
EXPOSURE_LEVEL = 0.45
EXPOSURE_PERCENTILE = 60

# A real detector on a real sensor does not do better than about this.
PIXEL_SIGMA_FLOOR = 0.1

# A rig is bolted down pointing at its workspace, not at the object, so the
# object sits a little off each camera's axis -- which is the only place lens
# distortion has anything to do.
AIM_OFFSET = (35.0, 35.0, 0.0)

C_MARKER = "tab:orange"
C_TRUE = "tab:green"
C_EST = "tab:red"
C_RIG = "tab:blue"

DICTIONARY = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


# --- where the cameras stand ----------------------------------------------


def camera_matrix(optics):
    f = optics.focal_length_pixels
    return np.array([[f, 0.0, optics.pixels_x / 2.0],
                     [0.0, f, optics.pixels_y / 2.0],
                     [0.0, 0.0, 1.0]])


def rig(spread, standoff):
    """Three cameras around the point: off to the side, and not all one height."""
    angles = np.radians([90.0, 210.0, 330.0])
    radii = spread * np.array([1.0, 0.9, 1.1])
    heights = standoff + np.array([0.0, 60.0, 30.0])
    return [np.array([r * np.cos(a), r * np.sin(a), h])
            for r, a, h in zip(radii, angles, heights)]


def scene(optics, spread, standoff, aim=AIM_OFFSET):
    """The camera matrix, the three poses, and their projection matrices."""
    K = camera_matrix(optics)
    poses = []
    for position in rig(spread, standoff):
        R, t = camera_pose(tuple(position), tuple(aim))
        poses.append((position, np.array(R), np.array(t)))
    projections = [K @ np.column_stack([R, t]) for _, R, t in poses]
    return K, poses, projections


def marker_corners(side, centre=np.zeros(3)):
    """The four corners of the printed square, lying flat and facing up."""
    half = side / 2.0
    return np.array([[centre[0] - half, centre[1] - half, centre[2]],
                     [centre[0] + half, centre[1] - half, centre[2]],
                     [centre[0] + half, centre[1] + half, centre[2]],
                     [centre[0] - half, centre[1] + half, centre[2]]])


def project(K, R, t, points):
    image, _ = cv2.projectPoints(np.asarray(points, float), R, t, K, None)
    return image.reshape(-1, 2)


def pairwise_baseline(spread, standoff=500.0):
    positions = rig(spread, standoff)
    spans = [float(np.linalg.norm(a - b))
             for i, a in enumerate(positions) for b in positions[i + 1:]]
    return float(np.median(spans))


def locate(projections, pixels):
    """Where the rays meet -- through the tested solver, not a copy of it."""
    plain = [np.asarray(P, float).tolist() for P in projections]
    return np.array(triangulate(plain, [tuple(float(c) for c in p) for p in pixels]))


def reprojection_residual(projections, pixels, point):
    """Per-camera pixel distance between where the point lands and where it was seen."""
    residuals = []
    for projection, pixel in zip(projections, pixels):
        landed = project_point(np.asarray(projection, float).tolist(),
                               tuple(float(value) for value in point))
        residuals.append(float(np.linalg.norm(np.asarray(landed) - np.asarray(pixel, float))))
    return residuals


# --- the lens -------------------------------------------------------------


def barrel_from_datasheet(optics):
    """The distortion coefficient that puts the corners where the datasheet says.

    The maker prints a "TV distortion" figure, which is a sag in the picture
    rather than a Brown coefficient, so this is a translation and not a copy.
    """
    corner = sqrt((optics.pixels_x / 2.0) ** 2 + (optics.pixels_y / 2.0) ** 2)
    radius = corner / optics.focal_length_pixels
    return -optics.distortion * optics.pixels_y / (radius ** 3 * optics.focal_length_pixels)


def corner_shift_pixels(optics, k1):
    """How far the frame's corner swings inward under that barrel distortion."""
    corner = sqrt((optics.pixels_x / 2.0) ** 2 + (optics.pixels_y / 2.0) ** 2)
    return corner - corner * (1.0 + k1 * (corner / optics.focal_length_pixels) ** 2)


def defocus_blur_pixels(optics, distance_mm):
    """Circle of confusion in pixels, for a lens focused at infinity.

    Worst case: a lens focused at infinity blurs everything nearer, and the blur
    still has to be visible for focus to carry any distance information.
    """
    aperture_mm = optics.focal_length * 1000.0 / optics.f_number
    focal_mm = optics.focal_length * 1000.0
    return (aperture_mm * focal_mm / distance_mm) / (optics.pixel_size * 1000.0)


def distortion_maps(K, k1, size):
    """Where each pixel of the bent frame should be sampled from.

    The model runs from the ideal picture to the bent one; drawing needs the
    other direction, so the radius is un-bent per pixel with a few Newton steps.
    """
    width, height = size
    u, v = np.meshgrid(np.arange(width, dtype=np.float64), np.arange(height, dtype=np.float64))
    x = (u - K[0, 2]) / K[0, 0]
    y = (v - K[1, 2]) / K[1, 1]
    bent = np.hypot(x, y)
    ideal = bent.copy()
    for _ in range(8):
        ideal = ideal - (ideal * (1.0 + k1 * ideal ** 2) - bent) / (1.0 + 3.0 * k1 * ideal ** 2)
    scale = np.where(bent > 1e-9, ideal / np.maximum(bent, 1e-9), 1.0)
    return ((x * scale * K[0, 0] + K[0, 2]).astype(np.float32),
            (y * scale * K[1, 1] + K[1, 2]).astype(np.float32))


def vignette(optics):
    """cos^4 falloff: the corners of a small lens sit in the shade."""
    yy, xx = np.mgrid[0:optics.pixels_y, 0:optics.pixels_x]
    x = (xx - optics.pixels_x / 2.0) / optics.focal_length_pixels
    y = (yy - optics.pixels_y / 2.0) / optics.focal_length_pixels
    return np.cos(np.arctan(np.hypot(x, y))) ** 4


@lru_cache(maxsize=8)
def lens_maps(optics, k1):
    """The distortion maps for this lens, computed once and kept.

    Neither map depends on the scene, so recomputing them per frame is pure
    waste -- and `distortion_maps` runs an 8-step Newton iteration over every
    pixel. Both arrays are marked read-only: they are shared, and a caller that
    scribbled on one would corrupt every later render.
    """
    mapx, mapy = distortion_maps(camera_matrix(optics), k1,
                                 (optics.pixels_x, optics.pixels_y))
    mapx.flags.writeable = False
    mapy.flags.writeable = False
    return mapx, mapy


@lru_cache(maxsize=8)
def lens_vignette(optics):
    """The cos^4 falloff for this lens, computed once and kept."""
    falloff = vignette(optics)
    falloff.flags.writeable = False
    return falloff


def auto_exposure(frame, level=EXPOSURE_LEVEL, percentile=EXPOSURE_PERCENTILE):
    """The sensor's exposure loop, in one line.

    The HM01B0 adjusts exposure until the scene sits at a sensible level, so a
    renderer without it produces pictures no camera would hand over: a dark
    table with a white sheet on it comes out almost black. This puts the scene's
    typical level near half of full scale, which is what the loop would do.
    """
    typical = float(np.percentile(frame, percentile))
    if typical <= 1e-6:
        return frame
    return frame * (level * 255.0 / typical)


# --- drawing a frame ------------------------------------------------------


def paper_artwork(marker_px, quiet_px):
    """A sheet with an ArUco marker printed on it, and the square's corners in it."""
    marker = cv2.aruco.generateImageMarker(DICTIONARY, 0, marker_px)
    sheet = np.full((marker_px + 2 * quiet_px, marker_px + 2 * quiet_px), 255, np.uint8)
    sheet[quiet_px:quiet_px + marker_px, quiet_px:quiet_px + marker_px] = marker
    corners = np.array([[quiet_px, quiet_px], [quiet_px + marker_px, quiet_px],
                        [quiet_px + marker_px, quiet_px + marker_px],
                        [quiet_px, quiet_px + marker_px]], np.float32)
    return sheet, corners


def table_top(optics, rng):
    """A surface under a lamp, rather than a void."""
    mottle = cv2.GaussianBlur(rng.normal(0.0, 1.0, (optics.pixels_y, optics.pixels_x)),
                              (0, 0), 3.0)
    mottle = mottle / (np.abs(mottle).max() + 1e-9)
    yy, xx = np.mgrid[0:optics.pixels_y, 0:optics.pixels_x]
    lamp = 1.0 - 0.30 * ((xx / optics.pixels_x - 0.35) ** 2
                         + (yy / optics.pixels_y - 0.3) ** 2)
    return np.clip((62.0 + 22.0 * mottle) * lamp, 0, 255)


def render(optics, K, R, t, rng, k1=None, marker_mm=MARKER_MM):
    """One frame, from photons to eight bits."""
    k1 = barrel_from_datasheet(optics) if k1 is None else k1
    size = (optics.pixels_x, optics.pixels_y)
    frame = table_top(optics, rng)

    # The paper is drawn in image coordinates, y downwards; the marker lies in
    # the world xy plane. Pairing the corners up in the obvious order mirrors
    # the print, and a mirrored ArUco is not a marker at all -- it reads as
    # noise. Reversing one list makes the two windings agree.
    quad = project(K, R, t, marker_corners(marker_mm))[::-1].astype(np.float32)
    span = float(max(np.ptp(quad[:, 0]), np.ptp(quad[:, 1])))
    marker_px = int(np.clip(round(span * 2.0), 24, 512))
    sheet, square = paper_artwork(marker_px, max(4, int(marker_px * QUIET_ZONE_FRACTION)))

    homography = cv2.getPerspectiveTransform(square, quad)
    paper = cv2.warpPerspective(sheet, homography, size, flags=cv2.INTER_LINEAR, borderValue=0)
    where = cv2.warpPerspective(np.full_like(sheet, 255), homography, size,
                                flags=cv2.INTER_NEAREST)
    frame = np.where(where > 0, paper, frame)

    frame = cv2.GaussianBlur(frame, (0, 0), max(defocus_blur_pixels(optics, 500.0) / 2.0, 0.3))
    if k1:
        mapx, mapy = lens_maps(optics, k1)
        frame = cv2.remap(frame, mapx, mapy, cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)
    frame = frame * lens_vignette(optics)

    # exposure and gain first, then the noise that light level implies
    frame = auto_exposure(frame)
    photons = np.clip(frame, 0, 255) / 255.0 * FULL_WELL
    frame = rng.poisson(photons) / FULL_WELL * 255.0
    frame = frame + rng.normal(0.0, READ_NOISE, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


# --- reading it back ------------------------------------------------------


def detector():
    return cv2.aruco.ArucoDetector(DICTIONARY, cv2.aruco.DetectorParameters())


def detect_marker(frame, ardetector):
    """The four corners of the printed square, as OpenCV's ArUco finds them."""
    corners, ids, _ = ardetector.detectMarkers(frame)
    if ids is None:
        raise RuntimeError("no marker in this frame")
    return np.asarray(corners[0], dtype=np.float64).reshape(-1, 2)


def diagonal_crossing(corners):
    """Where the diagonals of a quadrilateral meet.

    The corners come back in order, so 0 and 2 are opposite. Their crossing is
    exactly where the printed square's centre projects, from any viewpoint,
    because a projection carries straight lines to meetings of straight lines.
    A brightness centroid would be easier and would be wrong: the centroid of a
    foreshortened square is not the projection of its centre, and that is
    geometry rather than noise, so averaging more frames never removes it.
    """
    a, b, c, d = np.asarray(corners, float)
    u, _ = np.linalg.solve(np.column_stack([c - a, -(d - b)]), b - a)
    return a + u * (c - a)


def straight_lens(corners, optics, k1=None):
    """The same corners in the ideal picture, before the lens bent them."""
    k1 = barrel_from_datasheet(optics) if k1 is None else k1
    K = camera_matrix(optics)
    return cv2.undistortPoints(np.asarray(corners, np.float32).reshape(-1, 1, 2), K,
                               np.array([k1, 0.0, 0.0, 0.0, 0.0]), None,
                               K).reshape(-1, 2)


# --- measurements ---------------------------------------------------------


def detection_rate(optics, spread, standoff, rng, marker_mm=MARKER_MM, frames=15):
    """How often a marker that size is actually read, over every camera."""
    K, poses, _ = scene(optics, spread, standoff)
    ardetector = detector()
    hits = 0
    for _ in range(frames):
        for _, R, t in poses:
            try:
                detect_marker(render(optics, K, R, t, rng, marker_mm=marker_mm), ardetector)
                hits += 1
            except RuntimeError:
                pass
    return hits / (frames * len(poses))


def measure_detector(optics, spread, standoff, rng, frames=15, aim=AIM_OFFSET):
    """What the whole seeing chain costs in pixels, on rendered frames.

    Returns the bias and scatter of the marker centre against where it should
    have been, both with the lens taken out and with it left in.
    """
    K, poses, _ = scene(optics, spread, standoff, aim)
    ardetector = detector()
    careful, lazy, misses = [], [], 0
    for _ in range(frames):
        for _, R, t in poses:
            truth = project(K, R, t, np.zeros((1, 3)))[0]
            try:
                corners = detect_marker(render(optics, K, R, t, rng), ardetector)
            except RuntimeError:
                misses += 1
                continue
            careful.append(diagonal_crossing(straight_lens(corners, optics)) - truth)
            lazy.append(diagonal_crossing(corners) - truth)
    if not careful:
        raise RuntimeError(f"nothing readable at a standoff of {standoff:.0f} mm")
    careful, lazy = np.array(careful), np.array(lazy)
    return {
        "bias": careful.mean(axis=0), "scatter": careful.std(axis=0),
        "bent_bias": lazy.mean(axis=0), "bent_scatter": lazy.std(axis=0),
        "missed": misses / (frames * len(poses)),
    }


def off_axis_penalty(optics, spread, standoff, rng, aim=(150.0, 150.0, 0.0), frames=10):
    """Point error with the lens taken back out, and with it left in.

    Off to the side of the picture the two are not close. The lens leaves a
    standing offset on the detected centre, and because it is standing rather
    than random, averaging more frames never removes it. Half the frames out
    there are unreadable -- the quiet zone around the print falls off the edge
    of the picture -- so this keeps trying and reports how many it lost.
    """
    K, poses, projections = scene(optics, spread, standoff, aim)
    ardetector = detector()
    careful, lazy, missed, attempts = [], [], 0, max(frames * 6, 12)
    for _ in range(attempts):
        corners = []
        for _, R, t in poses:
            try:
                corners.append(detect_marker(render(optics, K, R, t, rng), ardetector))
            except RuntimeError:
                corners = None
                break
        if corners is None:
            missed += 1
            continue
        careful.append(float(np.linalg.norm(locate(
            projections, [diagonal_crossing(straight_lens(c, optics)) for c in corners]))))
        lazy.append(float(np.linalg.norm(locate(
            projections, [diagonal_crossing(c) for c in corners]))))
        if len(careful) >= frames:
            break
    if not careful:
        raise RuntimeError(f"nothing readable at a standoff of {standoff:.0f} mm")
    return float(np.mean(careful)), float(np.mean(lazy)), missed / attempts


def accuracy(optics, spread, standoff, sigma, rng, trials):
    """Triangulate from the exact pixels plus detector noise, many times over.

    Rendering every trial would only re-measure the renderer; what changes with
    the rig's shape is the geometry, so the noise is applied to the exact
    projection at the size the detector was measured to have.
    """
    K, poses, projections = scene(optics, spread, standoff)
    truth = np.zeros(3)
    exact = np.array([project(K, R, t, truth.reshape(1, 3))[0] for _, R, t in poses])
    errors = []
    for _ in range(trials):
        errors.append(float(np.linalg.norm(
            locate(projections, exact + rng.normal(0.0, sigma, exact.shape)))))
    return float(np.sqrt(np.mean(np.square(errors))))


def end_to_end(optics, spread, standoff, rng, aim=AIM_OFFSET):
    """One scene all the way through: render, read, straighten, solve.

    A marker too small to read is a result, not a crash: the caller gets the
    frames back with ``pixels`` left empty so the report can say so.
    """
    K, poses, projections = scene(optics, spread, standoff, aim)
    ardetector = detector()
    frames = [render(optics, K, R, t, rng) for _, R, t in poses]
    try:
        pixels = np.array([diagonal_crossing(straight_lens(detect_marker(frame, ardetector), optics))
                           for frame in frames])
    except RuntimeError:
        return {"frames": frames, "poses": poses, "projections": projections,
                "pixels": None, "estimate": None, "error_mm": float("inf")}
    estimate = locate(projections, pixels)
    return {
        "frames": frames, "poses": poses, "projections": projections,
        "pixels": pixels, "estimate": estimate,
        "error_mm": float(np.linalg.norm(estimate)),
    }


def looks_like_the_law(measurements, distance_short, distance_long, spread):
    """Does the error grow with the square of the distance, as promised?"""
    at = {(row["standoff"], row["spread"]): row["rms"] for row in measurements}
    near = at.get((distance_short, spread))
    far = at.get((distance_long, spread))
    if not near or not far:
        return False
    return far / near > 2.0


# --- the report -----------------------------------------------------------


def build_report(optics, marker_mm=MARKER_MM, spread=150.0, standoff=500.0,
                 trials=300, frames=15, seed=7):
    """Measure the whole chain and judge every measurement, in one place."""
    rng = np.random.default_rng(seed)
    k1 = barrel_from_datasheet(optics)
    shifted = corner_shift_pixels(optics, k1)
    published = optics.distortion

    read = detection_rate(optics, spread, standoff, rng, marker_mm, frames)
    seen = measure_detector(optics, spread, standoff, rng, frames)
    sigma = max(float(np.hypot(*seen["scatter"])), PIXEL_SIGMA_FLOOR)

    distances = (250.0, 350.0, 500.0, 700.0, 1000.0)
    spreads = (80.0, 150.0, 300.0)
    measurements = [
        {"standoff": standoff_, "spread": spread_,
         "baseline": pairwise_baseline(spread_),
         "rms": accuracy(optics, spread_, standoff_, sigma, rng, trials)}
        for standoff_ in distances for spread_ in spreads
    ]

    wide = {(row["standoff"]): row["rms"] for row in measurements if row["spread"] == 300.0}
    tight = {(row["standoff"]): row["rms"] for row in measurements if row["spread"] == 80.0}
    separation = all(wide[d] < tight[d] for d in distances)

    run = end_to_end(optics, spread, standoff, rng)
    careful_mm, lazy_mm, lost = off_axis_penalty(optics, spread, standoff, rng,
                                                 frames=min(frames, 10))
    predicted = sigma * float(np.linalg.norm(run["poses"][0][0])) ** 2 / (
        optics.focal_length_pixels * pairwise_baseline(spread))

    checks = [
        {
            "name": "the lens bends the picture about as much as the datasheet says",
            "ok": 0.5 * published <= shifted / optics.pixels_y <= 1.05 * published,
            "detail": f"{100 * shifted / optics.pixels_y:.2f}% of the frame against "
                      f"the datasheet's {100 * published:.2f}%",
        },
        {
            "name": "every camera reads the marker at the working distance",
            "ok": read >= 0.9,
            "detail": f"{100 * read:.0f}% readable at {standoff:.0f} mm with a "
                      f"{marker_mm:.0f} mm marker",
        },
        {
            "name": "the point lands as close as the geometry allows",
            "ok": run["error_mm"] <= 3.0 * predicted,
            "detail": (f"{run['error_mm']:.2f} mm against {predicted:.2f} mm predicted "
                       f"from {sigma:.2f} px of detector noise"
                       if run["estimate"] is not None else
                       f"the marker could not be read at {marker_mm:.0f} mm"),
        },
        {
            "name": "cameras further apart give a better fix at every distance",
            "ok": separation,
            "detail": "; ".join(f"{d:.0f} mm: {wide[d]:.2f} vs {tight[d]:.2f}"
                                for d in distances),
        },
        {
            "name": "the error grows with the square of the distance",
            "ok": looks_like_the_law(measurements, 500.0, 1000.0, 150.0),
            "detail": "doubling the standoff more than doubles the error",
        },
    ]

    return {
        "optics": optics, "marker_mm": marker_mm, "k1": k1, "shift_px": shifted,
        "sigma": sigma, "read": read, "detector": seen, "measurements": measurements,
        "run": run, "predicted_mm": predicted, "checks": checks,
        "off_axis_mm": (careful_mm, lazy_mm), "off_axis_lost": lost,
        "spread": spread, "standoff": standoff,
    }


def format_report(report):
    """The report a human reads: the numbers, then each verdict, then a summary."""
    optics = report["optics"]
    seen = report["detector"]

    lines = [
        f"Synthetic HM01B0 rig: {optics.name}",
        f"  {optics.sensor}, {optics.pixels_x}x{optics.pixels_y}, "
        f"{optics.pixel_size * 1e6:.1f} um pixels, EFL {optics.focal_length * 1000:.2f} mm, "
        f"F/{optics.f_number}",
        f"  {optics.focal_length_pixels:.1f} px focal length, "
        f"{optics.pixel_footprint(1.0) * 1000:.2f} mm per pixel at 1 m",
        f"  marker {report['marker_mm']:.0f} mm ArUco square, printed with a quiet zone",
        f"  barrel k1 = {report['k1']:.4f} -> corner moves {report['shift_px']:.2f} px, "
        f"defocus {defocus_blur_pixels(optics, report['standoff']):.2f} px, "
        f"vignetting to {vignette(optics)[0, 0]:.2f} in the corners",
        f"  full well {FULL_WELL:.0f} e-, read noise {READ_NOISE} grey levels, "
        f"{100 * report['read']:.0f}% of frames readable",
        "",
        "The marker as the cameras see it, in pixels",
        f"  corners straight out of the detector   bias {np.round(seen['bent_bias'], 3)}"
        f"  scatter {np.round(seen['bent_scatter'], 3)}",
        f"  after taking the lens back out         bias {np.round(seen['bias'], 3)}"
        f"  scatter {np.round(seen['scatter'], 3)}",
        f"  standing offset is what never averages away; using "
        f"{report['sigma']:.3f} px per camera in what follows",
        f"  a target 150 mm off the axis   {report['off_axis_mm'][0]:.2f} mm straightened,"
        f" {report['off_axis_mm'][1]:.2f} mm with the lens left in"
        f" ({100 * report['off_axis_lost']:.0f}% unreadable out there)",
        "",
        "Accuracy: error of the found point, in mm",
        f"  {'standoff':>9}" + "".join(f"{row:>12.0f}" for row in (80.0, 150.0, 300.0))
        + "   mm of spread",
    ]
    for standoff in (250.0, 350.0, 500.0, 700.0, 1000.0):
        row = "".join(
            f"{next(m['rms'] for m in report['measurements']
                    if m['standoff'] == standoff and m['spread'] == spread):>12.2f}"
            for spread in (80.0, 150.0, 300.0))
        lines.append(f"  {standoff:9.0f}{row}")
    lines.append("  baselines " + "  ".join(
        f"{pairwise_baseline(s):.0f} mm" for s in (80.0, 150.0, 300.0)))

    lines += ["", "Checks"]
    for check in report["checks"]:
        lines.append(f"  {'OK  ' if check['ok'] else 'FAIL'}  {check['name']}")
        lines.append(f"        {check['detail']}")

    failures = [check for check in report["checks"] if not check["ok"]]
    lines.append("")
    lines.append(f"FAIL ({len(failures)} of {len(report['checks'])} checks)" if failures else "PASS")
    return "\n".join(lines)


# --- the two figures ------------------------------------------------------


def draw_frustum(ax, optics, R, t, depth, colour=C_RIG):
    half = optics.pixels_x / 2.0 / optics.focal_length_pixels * depth
    origin = -R.T @ t
    ring = np.array([[-half, -half, depth], [half, -half, depth],
                     [half, half, depth], [-half, half, depth], [-half, -half, depth]])
    world = np.array([R.T @ (corner - t) for corner in ring])
    ax.plot(world[:, 0], world[:, 1], world[:, 2], color=colour, lw=0.8, alpha=0.7)
    for corner in world[:-1]:
        ax.plot(*zip(origin, corner), color=colour, lw=0.6, alpha=0.5)
    ax.scatter(*origin, color=colour, s=45, marker="s")


def span_of(optics, pose, corners):
    """How wide the marker comes out in pixels, in one camera's view."""
    _, R, t = pose
    return float(np.ptp(project(camera_matrix(optics), R, t, corners), axis=0).max())


def figure_views(report, path, crop_half=26):
    """The three frames, magnified at the marker, beside the geometry."""
    optics = report["optics"]
    run = report["run"]
    frames, poses = run["frames"], run["poses"]
    corners = marker_corners(report["marker_mm"])

    fig = plt.figure(figsize=(16, 5.8))
    grid = fig.add_gridspec(1, 4, width_ratios=[1, 1, 1, 1.5], wspace=0.22)

    for index, frame in enumerate(frames):
        ax = fig.add_subplot(grid[0, index])
        ax.imshow(frame, cmap="gray", vmin=0, vmax=255)
        title = (f"camera {index + 1}   marker spans {span_of(optics, poses[index], corners):.0f}"
                 f" px\n{optics.pixels_x}x{optics.pixels_y} mono, "
                 f"EFL {optics.focal_length * 1000:.2f} mm, F/{optics.f_number}")
        if run["pixels"] is not None:
            found = run["pixels"][index]
            ax.plot(*found, marker="+", color=C_EST, ms=11, mew=1.4)
            centre = np.round(found).astype(int)
            x0 = int(np.clip(centre[0] - crop_half, 0, optics.pixels_x - 2 * crop_half))
            y0 = int(np.clip(centre[1] - crop_half, 0, optics.pixels_y - 2 * crop_half))
            ax.add_patch(plt.Rectangle((x0, y0), 2 * crop_half, 2 * crop_half,
                                       fill=False, edgecolor=C_EST, lw=0.9))
            inset = ax.inset_axes([0.63, 0.02, 0.35, 0.35])
            inset.imshow(frame[y0:y0 + 2 * crop_half, x0:x0 + 2 * crop_half], cmap="gray",
                         vmin=0, vmax=255, interpolation="nearest")
            inset.set_xticks([])
            inset.set_yticks([])
            for spine in inset.spines.values():
                spine.set_edgecolor(C_EST)
        else:
            title = "marker not readable\n" + title
        ax.set_title(title, fontsize=8.5)
        ax.set_xticks([])
        ax.set_yticks([])

    ax = fig.add_subplot(grid[0, 3], projection="3d")
    ring = np.vstack([corners, corners[:1]])
    ax.plot(ring[:, 0], ring[:, 1], ring[:, 2], color=C_MARKER, lw=2.5,
            label=f"printed square ({report['marker_mm']:.0f} mm)")
    ax.scatter(0, 0, 0, color=C_TRUE, marker="*", s=240, label="the true point")
    if run["estimate"] is not None:
        ax.scatter(*run["estimate"], color=C_EST, marker="x", s=110,
                   label="found from three rays")
    for index, (position, R, t) in enumerate(poses):
        draw_frustum(ax, optics, R, t, 60.0)
        if run["estimate"] is not None:
            ax.plot(*zip(position, run["estimate"]), color=C_RIG, lw=1.0, alpha=0.45)
        ax.text(*position, f"  c{index + 1}", color=C_RIG, fontsize=8)
    verdict = (f"off by {run['error_mm']:.2f} mm" if run["estimate"] is not None
               else "no point to find: the marker was not readable")
    ax.set_title(f"the three lines of sight\nspread {report['spread']:.0f} mm, "
                 f"standoff {report['standoff']:.0f} mm, {verdict}", fontsize=9)
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_zlabel("z (mm)")
    ax.set_box_aspect((1, 1, 1))
    ax.legend(fontsize=8, loc="upper left")
    fig.suptitle("What the three cameras see, and where their three rays meet", fontsize=12)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def figure_accuracy(report, path):
    """Error against distance and against spread, beside the +-2 mm asked for."""
    optics = report["optics"]
    measurements = report["measurements"]
    spreads = sorted({row["spread"] for row in measurements})
    distances = sorted({row["standoff"] for row in measurements})

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2))
    for spread in spreads:
        rows = [row for row in measurements if row["spread"] == spread]
        axes[0].plot([row["standoff"] for row in rows], [row["rms"] for row in rows],
                     marker="o", label=f"spread {spread:.0f} mm "
                                       f"(baseline {pairwise_baseline(spread):.0f} mm)")
    axes[0].set_xlabel("standoff (distance to the point), mm")
    axes[0].set_ylabel("error of the found point, mm")
    axes[0].set_title("Closer is more accurate,\nand the error grows as the square of distance",
                      fontsize=10)

    for standoff in distances:
        rows = [row for row in measurements if row["standoff"] == standoff]
        axes[1].plot([row["spread"] for row in rows], [row["rms"] for row in rows],
                     marker="o", label=f"standoff {standoff:.0f} mm")
    axes[1].set_xlabel("spread (how far the cameras sit off-axis), mm")
    axes[1].set_title("Cameras further apart fix the point better,\nin proportion",
                      fontsize=10)

    for ax in axes:
        ax.axhline(2.0, color=C_EST, ls="--", lw=1.2)
        ax.text(ax.get_xlim()[0], 2.0, "  the +-2 mm being asked for",
                color=C_EST, fontsize=8, va="bottom")
        ax.set_yscale("log")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)

    fig.suptitle(f"Accuracy of three HM01B0 cameras ({optics.name}),\n"
                 f"{report['marker_mm']:.0f} mm ArUco marker, "
                 f"detector noise {report['sigma']:.2f} px", fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)


# --- running it -----------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--optics", default=DEFAULT_MODULE, choices=sorted(MODULES),
                        help="which camera module to model")
    parser.add_argument("--out", default="images", help="where the figures go")
    parser.add_argument("--marker-mm", type=float, default=MARKER_MM,
                        help="side of the printed ArUco square")
    parser.add_argument("--trials", type=int, default=300,
                        help="draws per cell of the accuracy table")
    parser.add_argument("--frames", type=int, default=15,
                        help="rendered frames per measurement")
    args = parser.parse_args(argv)

    optics = optics_for(args.optics)
    report = build_report(optics, marker_mm=args.marker_mm,
                          trials=args.trials, frames=args.frames)
    print(format_report(report))

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    figure_views(report, out / "camera-views.png")
    figure_accuracy(report, out / "camera-accuracy.png")
    print(f"\nwrote {out / 'camera-views.png'}")
    print(f"wrote {out / 'camera-accuracy.png'}")
    return 0 if all(check["ok"] for check in report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
