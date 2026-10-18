"""Board geometry and the OpenCV-sample fetch, with no OpenCV in sight.

Kept apart from ``check_stereo_sample`` so the maths that decides whether a
reconstruction is good -- board layout, ``planarity``, "is every corner one
square from its neighbour", "does this calibration match the published one" --
stays importable and testable on a machine that has never installed OpenCV.
"""

import pathlib
import urllib.request

import numpy as np

# The sample is 13 pairs, not 14: OpenCV never published a tenth one, and a
# request for it answers with an error page wearing a .jpg file name.
SAMPLE_IMAGE_INDICES = (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14)
SAMPLE_BASE_URL = "https://raw.githubusercontent.com/opencv/opencv/master/samples/data"

# Board and grid as declared by the sample's own left_intrinsics.yml.
BOARD_COLS = 9
BOARD_ROWS = 6
SQUARE_SIZE = 0.025  # metres

DEFAULT_REL_TOL = 0.01
DEFAULT_ABS_TOL_PX = 5.0

JPEG_MAGIC = b"\xff\xd8\xff"


def sample_names(indices=SAMPLE_IMAGE_INDICES):
    """The sample files worth fetching, in the order they are used."""
    names = []
    for index in indices:
        names.append(f"left{index:02d}.jpg")
        names.append(f"right{index:02d}.jpg")
    names.append("left_intrinsics.yml")
    return names


def download_sample(cache_dir, base_url=SAMPLE_BASE_URL, names=None):
    """Fetch the sample into ``cache_dir``, leaving files that are already there.

    Anything that does not arrive as the kind of file its name claims raises,
    so a server's error page can never be quietly taken for an image.
    """
    cache_dir = pathlib.Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    names = sample_names() if names is None else list(names)

    written = []
    for name in names:
        target = cache_dir / name
        if not target.exists():
            url = f"{base_url.rstrip('/')}/{name}"
            with urllib.request.urlopen(url) as response:
                data = response.read()
            if name.endswith(".jpg") and not data.startswith(JPEG_MAGIC):
                raise RuntimeError(
                    f"{url} did not return a JPEG (starts with {data[:20]!r}); "
                    f"the sample file {name} may no longer exist upstream"
                )
            target.write_bytes(data)
        written.append(target)
    return written


def compare_intrinsics(reference, measured, rel_tol=DEFAULT_REL_TOL, abs_tol_px=DEFAULT_ABS_TOL_PX):
    """Report the intrinsics that disagree by more than both tolerances.

    Judging on a relative and an absolute measure at once lets one rule cover
    the focal length, whose meaningful scale is a proportion, and the principal
    point, which a planar target pins down only to a few pixels.

    Returns one dict per offending value; an empty list means agreement.
    """
    complaints = []
    for name in ("fx", "fy", "cx", "cy"):
        want = float(reference[name])
        got = float(measured[name])
        abs_error = abs(got - want)
        rel_error = abs_error / abs(want) if want else float("inf")
        if rel_error > rel_tol and abs_error > abs_tol_px:
            complaints.append(
                {
                    "name": name,
                    "reference": want,
                    "measured": got,
                    "abs_error": abs_error,
                    "rel_error": rel_error,
                }
            )
    return complaints


def projection_matrix(K, R, t):
    """The 3x4 camera matrix ``K [R | t]`` that maps board points to pixels."""
    K = np.asarray(K, dtype=float)
    R = np.asarray(R, dtype=float)
    t = np.asarray(t, dtype=float).reshape(3)
    return K @ np.hstack([R, t.reshape(3, 1)])


def object_points(cols, rows, square):
    """The board's inner corners in its own frame, in metres.

    Row-major, x along the columns and y along the rows, which is the order
    ``cv2.findChessboardCorners`` reports corners in.
    """
    if cols < 2 or rows < 2:
        raise ValueError(f"a board needs at least 2 by 2 corners, got {cols} by {rows}")
    if square <= 0:
        raise ValueError(f"square size must be positive, got {square}")
    grid = np.zeros((rows, cols, 3))
    grid[:, :, 0] = np.arange(cols) * square
    grid[:, :, 1] = np.arange(rows)[:, None] * square
    return grid.reshape(-1, 3)


def grid_side_lengths(points, cols, rows):
    """Distance between every pair of neighbouring corners of the board.

    A correctly triangulated board is a rigid grid, so all of these should
    come out equal; the spread is what betrays a bad reconstruction.
    """
    points = np.asarray(points, dtype=float)
    expected = cols * rows
    if points.shape != (expected, 3):
        raise ValueError(f"expected {expected} board points, got shape {points.shape}")
    grid = points.reshape(rows, cols, 3)
    horizontal = np.linalg.norm(grid[:, 1:] - grid[:, :-1], axis=2).ravel()
    vertical = np.linalg.norm(grid[1:, :] - grid[:-1, :], axis=2).ravel()
    return np.concatenate([horizontal, vertical])


def fit_plane(points):
    """Least-squares plane through ``points``, returned as ``(centroid, unit normal)``.

    The normal's sign is arbitrary -- a plane has two of them -- so callers
    must not read anything into it.
    """
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[0] < 3:
        raise ValueError(f"a plane needs at least three points, got {len(points)}")
    centroid = points.mean(axis=0)
    _, singular, right = np.linalg.svd(points - centroid)
    if singular[1] <= 1e-9 * singular[0]:
        raise ValueError("points are collinear: no plane is defined")
    return centroid, right[2]


def planar_residuals(points, centroid, normal):
    """Unsigned distance of each point from the plane.

    ``normal`` must already be a unit vector.
    """
    points = np.asarray(points, dtype=float)
    normal = np.asarray(normal, dtype=float)
    return np.abs((points - np.asarray(centroid, dtype=float)) @ normal)
