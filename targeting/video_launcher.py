"""Start and stop the external camera-stream sender.

Slicer's Python has no OpenCV, so the sender runs in the project environment and
Slicer only collects the paths. This module owns the command line and the child
process; it imports no Slicer or Qt, which is what keeps it testable on the host.
"""

from __future__ import annotations

import math
import os
import pathlib
import signal
import subprocess


class LauncherError(ValueError):
    """The requested sender cannot be started."""


def validate_video_files(paths):
    """Return three existing clip paths, or refuse with a named reason."""
    files = tuple(pathlib.Path(path) for path in paths)
    if len(files) != 3:
        raise LauncherError("choose exactly three video files (C1, C2, C3)")
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise LauncherError("missing video file: " + ", ".join(missing))
    return files


def build_command(video_files, *, calibration=None, truth=None, fps=8.0,
                  port=18944, uv="uv"):
    """The sender command for three explicit clips and optional inputs."""
    files = validate_video_files(video_files)
    try:
        rate = float(fps)
    except (TypeError, ValueError):
        raise LauncherError("fps must be a number") from None
    if not math.isfinite(rate) or rate <= 0:
        raise LauncherError("fps must be greater than zero")
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        raise LauncherError("port must be a whole number") from None
    if not 1 <= port_number <= 65535:
        raise LauncherError("port must be between 1 and 65535")

    command = [str(uv), "run", "tools/camera_stream.py", "--video-files",
               *(str(path) for path in files),
               "--fps", f"{rate:g}", "--port", str(port_number)]
    for flag, path in (("--calibration", calibration), ("--truth", truth)):
        if not path:
            continue
        candidate = pathlib.Path(path)
        if not candidate.is_file():
            raise LauncherError(
                f"missing {flag.lstrip('-')} file: {candidate}")
        command.extend([flag, str(candidate)])
    return command


class VideoProcess:
    """One external sender process, with a log and a clean stop."""

    def __init__(self, command, *, cwd, log_path, popen=None):
        self.command = tuple(str(part) for part in command)
        self.cwd = pathlib.Path(cwd)
        self.log_path = pathlib.Path(log_path)
        self._popen = subprocess.Popen if popen is None else popen
        self._process = None
        self._log = None
        self._returncode = None
        self._stopped = False

    def start(self):
        """Spawn the sender, replacing any finished child, and report status."""
        if self.is_running():
            return self.poll()
        self._close_log()
        self._process = None
        self._returncode = None
        self._stopped = False
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("wb")
        try:
            self._process = self._popen(
                list(self.command),
                cwd=str(self.cwd),
                stdout=self._log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_log()
            self._process = None
            raise LauncherError(
                f"could not start the video sender: {exc}") from exc
        return self.poll()

    def poll(self):
        """Non-blocking status: off, running, stopped, or exited."""
        if self._process is None:
            return self._status("off", "video: off")
        code = self._process.poll()
        if code is None:
            return self._status(
                "running", f"video: running (pid {self._process.pid})",
                pid=self._process.pid)
        self._returncode = code
        self._close_log()
        if self._stopped:
            return self._status("stopped", "video: stopped", returncode=code)
        return self._status(
            "exited",
            f"video: sender exited (code {code}); see {self.log_path}",
            returncode=code, log_tail=self.log_tail())

    def status(self):
        return self.poll()

    def is_running(self):
        return self._process is not None and self._process.poll() is None

    def stop(self, timeout=5.0):
        """Terminate the sender's process group and report the stop."""
        process = self._process
        if process is None:
            return self._status("off", "video: off")
        self._stopped = True
        if process.poll() is None:
            self._terminate(process, timeout)
        self._returncode = process.returncode
        self._close_log()
        return self._status(
            "stopped", "video: stopped", returncode=self._returncode)

    def log_tail(self, lines=6):
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-int(lines):])

    def _terminate(self, process, timeout):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (AttributeError, OSError):
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (AttributeError, OSError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    def _close_log(self):
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def _status(self, status, message, **extra):
        payload = {
            "status": status,
            "message": message,
            "pid": None,
            "returncode": None,
            "log_path": str(self.log_path),
            "log_tail": "",
        }
        payload.update(extra)
        return payload
