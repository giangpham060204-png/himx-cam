"""The point a human is currently pointing at, as Slicer last published it.

Slicer writes this file while the operator moves a probe; the camera sandbox
reads it and moves its lines of sight to match. A file rather than a socket,
because a file is the only channel this project has ever needed — and because a
stale file is harmless while a half-written one is not. So the writer replaces
the file atomically (write a temporary name, then rename over the old one), and
this reader refuses anything it cannot parse rather than guessing at it: a probe
that cannot be read must say so, because the point it would otherwise produce is
one a human proposed, and a wrong one is worse than none.

Pure standard library: Slicer writes it with Python's own json, and the tools
read it, and neither may need a third-party package to do so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# The name the writer and both readers agree on. One definition.
PROBE_FILE = "slicer-probe.json"


@dataclass(frozen=True)
class Probe:
    """One published probe position, with enough context to name it later."""

    cube: tuple
    ras: tuple
    view: str = ""       # which slice view the operator was in, if any
    when: str = ""       # the writer's timestamp, as text


def read_probe(path) -> tuple:
    """``(Probe | None, note)`` from one published file.

    The note says what was read on success and why nothing was on failure. A
    missing file is a sentence, not a traceback: the sandbox is often opened
    before Slicer has published anything.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"no probe at {path.name} ({exc})"
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return None, f"{path.name}: not a probe file ({exc})"
    if not isinstance(payload, dict):
        return None, f"{path.name}: not a probe file (expected an object)"
    missing = [name for name in ("cube", "ras") if name not in payload]
    if missing:
        return None, f"{path.name}: missing {', '.join(missing)}"
    try:
        cube = tuple(float(value) for value in payload["cube"])
        ras = tuple(float(value) for value in payload["ras"])
    except (TypeError, ValueError):
        return None, f"{path.name}: cube and ras must be three numbers each"
    if len(cube) != 3 or len(ras) != 3:
        return None, f"{path.name}: cube and ras must be three numbers each"
    return Probe(cube=cube, ras=ras,
                 view=str(payload.get("view", "")),
                 when=str(payload.get("when", ""))), f"{path.name}: probe read"
