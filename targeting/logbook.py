"""Append-only CSV log of placed targets.

One file per day, created on first use, with a header row. Flushed after
every append so a crash cannot lose accepted targets. `when` is injectable
so tests are deterministic. Pure stdlib: no Slicer, no hidden state.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HEADER = (
    "timestamp",
    "view",
    "u_px",
    "v_px",
    "depth_mm",
    "cube_x",
    "cube_y",
    "cube_z",
    "ras_r",
    "ras_a",
    "ras_s",
)


FILE_PREFIX = "targets-"


def log_path(logs_dir, when=None) -> Path:
    when = when or datetime.now()
    return Path(logs_dir) / f"{FILE_PREFIX}{when:%Y-%m-%d}.csv"


def format_row(*, view, u_px, v_px, depth_mm, cube, ras, when=None) -> str:
    when = when or datetime.now()
    return ",".join(
        (
            when.isoformat(timespec="seconds"),
            view,
            f"{u_px:.1f}",
            f"{v_px:.1f}",
            f"{depth_mm:.3f}",
            *(f"{value:.3f}" for value in cube),
            *(f"{value:.3f}" for value in ras),
        )
    )


def append_target(
    logs_dir, *, view, u_px, v_px, depth_mm, cube, ras, when=None
) -> Path:
    """Append one accepted target; returns the file it went into.

    Raises OSError when the location cannot be written, so the caller can
    tell the operator instead of losing the record silently.
    """
    path = log_path(logs_dir, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    # An existing file that is empty -- touched but never written -- holds no
    # header, so the first row would land headerless and every later read
    # would misparse it. Treat it as new.
    new_file = not path.exists() or path.stat().st_size == 0
    row = format_row(
        view=view,
        u_px=u_px,
        v_px=v_px,
        depth_mm=depth_mm,
        cube=cube,
        ras=ras,
        when=when,
    )
    with path.open("a", encoding="utf-8") as handle:
        if new_file:
            handle.write(",".join(HEADER) + "\n")
        handle.write(row + "\n")
        handle.flush()
    return path


@dataclass(frozen=True)
class Target:
    """One accepted screen click, as the module recorded it."""

    when: str
    view: str
    u_px: float
    v_px: float
    depth_mm: float
    cube: tuple
    ras: tuple


@dataclass(frozen=True)
class TargetLog:
    """What a file held: the targets that read, and a note per row that did not."""

    targets: tuple
    problems: tuple        # human-readable, each already naming file and line


def read_targets(path) -> TargetLog:
    """Every readable target in one file, and a note for every row lost.

    Columns are matched by name, so a column added later cannot shift what the
    ones after it mean; a file missing one of the declared columns is rejected
    whole, because reading it anyway would produce plausible wrong numbers.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return TargetLog((), (f"{path.name}: cannot read ({exc})",))

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return TargetLog((), (f"{path.name}: empty file",))
    missing = [name for name in HEADER if name not in reader.fieldnames]
    if missing:
        return TargetLog((), (f"{path.name}: missing column(s) {', '.join(missing)}",))

    targets, problems = [], []
    for number, row in enumerate(reader, start=2):     # line 1 is the header
        try:
            targets.append(_parse_target(row))
        except (ValueError, TypeError, KeyError) as exc:
            problems.append(f"{path.name} line {number}: {exc}")
    return TargetLog(tuple(targets), tuple(problems))


def _parse_target(row) -> Target:
    if None in row:
        raise ValueError("more values than the header names")
    if None in row.values():
        raise ValueError("fewer values than the header names")
    return Target(
        when=row["timestamp"],
        view=row["view"],
        u_px=float(row["u_px"]),
        v_px=float(row["v_px"]),
        depth_mm=float(row["depth_mm"]),
        cube=tuple(float(row[name]) for name in ("cube_x", "cube_y", "cube_z")),
        ras=tuple(float(row[name]) for name in ("ras_r", "ras_a", "ras_s")),
    )


def newest_target(logs_dir) -> tuple:
    """The newest accepted click in a folder, and a note either way.

    Returns ``(target, note)``. The note names the file and the row count on
    success; on failure it says what was looked at and why nothing came of it.
    A folder that does not exist is a sentence, not a traceback: an operator
    pressing the key on a machine that has never placed a target needs to be
    told, not shown a stack.
    """
    logs_dir = Path(logs_dir)
    paths = sorted(logs_dir.glob(f"{FILE_PREFIX}*.csv"))
    if not paths:
        return None, f"no {FILE_PREFIX}*.csv in {logs_dir}"
    newest = paths[-1]
    log = read_targets(newest)
    if not log.targets:
        reason = log.problems[0] if log.problems else "no rows"
        return None, f"{newest.name}: {reason}"
    return log.targets[-1], f"{newest.name}: the newest of {len(log.targets)} rows"
