"""The camera system's own log: one row per accepted camera-derived target.

Kept apart from logbook.py, which records screen clicks. A camera point is not a
click -- it has three pixel observations instead of one view, no slice slider,
and a residual -- so it gets its own columns rather than pretending.

What is deliberately absent: the error against the true position. In simulation
that error is known; real hardware never knows it. This log holds only what a
real system could also write. The three pixel pairs are the measurement
evidence: a reader can rebuild the triangulation from them and check the answer
independently, which is what a research log is for.

Pure standard library: Slicer has no OpenCV, the tests have nothing, and both
must be able to read this.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HEADER = (
    "timestamp", "source", "cube_x", "cube_y", "cube_z",
    "ras_r", "ras_a", "ras_s", "residual_px",
    "u1_px", "v1_px", "u2_px", "v2_px", "u3_px", "v3_px",
)

# Read by name, never by position.
REQUIRED = HEADER

FILE_PREFIX = "camera-targets-"
CAMERA_COUNT = 3


@dataclass(frozen=True)
class CameraTarget:
    """One accepted target, as a real system could have recorded it."""

    timestamp: str
    source: str
    cube: tuple
    ras: tuple
    residual_px: float
    pixels: tuple          # one (u, v) per camera, in camera order


@dataclass(frozen=True)
class CameraLog:
    """What a file held: the targets that read, and a note per row that did not."""

    targets: tuple
    problems: tuple        # human-readable, each already naming file and line


def camera_log_path(logs_dir, when=None) -> Path:
    when = when or datetime.now()
    return Path(logs_dir) / f"{FILE_PREFIX}{when:%Y-%m-%d}.csv"


def format_camera_row(*, cube, ras, residual_px, pixels, source="simulator", when=None) -> str:
    when = when or datetime.now()
    flat = [value for pair in pixels for value in pair]
    return ",".join((
        when.isoformat(timespec="seconds"),
        source,
        *(f"{value:.3f}" for value in cube),
        *(f"{value:.3f}" for value in ras),
        f"{residual_px:.4f}",
        *(f"{value:.2f}" for value in flat),
    ))


def append_camera_target(logs_dir, *, cube, ras, residual_px, pixels,
                         source="simulator", when=None) -> Path:
    """Append one accepted camera target; returns the file it went into.

    Raises OSError when the location cannot be written, so the caller can tell
    the operator instead of losing the record silently.
    """
    path = camera_log_path(logs_dir, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    # An existing file that is empty -- touched but never written -- holds no
    # header, so the first row would land headerless and every later read
    # would misparse it. Treat it as new.
    new_file = not path.exists() or path.stat().st_size == 0
    row = format_camera_row(cube=cube, ras=ras, residual_px=residual_px,
                            pixels=pixels, source=source, when=when)
    with path.open("a", encoding="utf-8") as handle:
        if new_file:
            handle.write(",".join(HEADER) + "\n")
        handle.write(row + "\n")
    return path


def read_camera_targets(path) -> CameraLog:
    """Every readable target in one file, and a note for every row lost.

    Columns are matched by name, so adding a column later cannot shift the
    meaning of the ones after it. A file missing a required column is rejected
    whole, with a reason: reading it anyway would produce plausible wrong
    numbers, which is the worst outcome a log reader can have.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return CameraLog((), (f"{path.name}: cannot read ({exc})",))

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return CameraLog((), (f"{path.name}: empty file",))
    missing = [name for name in REQUIRED if name not in reader.fieldnames]
    if missing:
        return CameraLog((), (f"{path.name}: missing column(s) {', '.join(missing)}",))

    targets, problems = [], []
    for number, row in enumerate(reader, start=2):     # line 1 is the header
        try:
            targets.append(_parse_row(row))
        except (ValueError, TypeError, KeyError) as exc:
            problems.append(f"{path.name} line {number}: {exc}")
    return CameraLog(tuple(targets), tuple(problems))


def _parse_row(row):
    # csv puts a short row's missing values in as None, and a long row's extras
    # under a None key. Either way the row does not match its own header.
    # Deliberately NOT a length check: a file with a future extra column is
    # read happily, by name, and that is the whole point of reading by name.
    if None in row:
        raise ValueError(f"more than {len(HEADER)} values")
    if None in row.values():
        raise ValueError(f"fewer than {len(HEADER)} values")
    pixels = tuple(
        (float(row[f"u{index}_px"]), float(row[f"v{index}_px"]))
        for index in range(1, CAMERA_COUNT + 1)
    )
    return CameraTarget(
        timestamp=row["timestamp"],
        source=row["source"],
        cube=tuple(float(row[name]) for name in ("cube_x", "cube_y", "cube_z")),
        ras=tuple(float(row[name]) for name in ("ras_r", "ras_a", "ras_s")),
        residual_px=float(row["residual_px"]),
        pixels=pixels,
    )
