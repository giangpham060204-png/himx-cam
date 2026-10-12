"""Append-only CSV log of placed targets.

One file per day, created on first use, with a header row. Flushed after
every append so a crash cannot lose accepted targets. `when` is injectable
so tests are deterministic. Pure stdlib: no Slicer, no hidden state.
"""

from __future__ import annotations

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


def log_path(logs_dir, when=None) -> Path:
    when = when or datetime.now()
    return Path(logs_dir) / f"targets-{when:%Y-%m-%d}.csv"


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
    new_file = not path.exists()
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
