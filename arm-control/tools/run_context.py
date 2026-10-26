"""Audit context shared by the operator tools."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def build_context(
    root: Path,
    operator: str = "",
    hardware_serial: str = "",
) -> dict[str, object]:
    return {
        "commit": git_commit(root),
        "operator": operator or os.environ.get("ARM_OPERATOR", ""),
        "hardware_serial": hardware_serial
        or os.environ.get("ARM_HARDWARE_SERIAL", ""),
    }
