"""Cross-process controlled-stop flag for the arm host."""

from __future__ import annotations

from pathlib import Path


class StopFlag:
    """A file-backed stop request shared by host processes."""

    def __init__(self, path: Path | None = None):
        self.path = path

    def requested(self) -> bool:
        return self.path is not None and self.path.exists()

    def request(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("stop\n", encoding="utf-8")

    def clear(self) -> None:
        if self.path is None:
            return
        self.path.unlink(missing_ok=True)
