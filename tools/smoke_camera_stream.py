"""Slicer smoke: video arrives, freezes on pause, and refuses uncalibrated clicks.

    timeout 300 <slicer> --no-splash --python-script tools/smoke_camera_stream.py

The sender runs as a child process through `uv`, so this proves the video path:
VideoFileSource -> pyigtl -> OpenIGTLinkIF -> three slice views -> pause ->
click -> an honest "uncalibrated" result -> resume. It is deliberately outside
pytest: Slicer has no pyigtl, and this is the one place that exercises both
sides together.
"""

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import traceback


def _resolve_project_root(_globals=None, _env=None):
    """Checkout root without scanning the disk (same rule as the tools)."""
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
        f"to the directory containing frame.json. __file__={here!r}")


PROJECT_ROOT = _resolve_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
_MODULE_DIR = PROJECT_ROOT / "SlicerTargeting" / "Targeting"
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

_CHECKS = []


def check(name, ok, detail=""):
    _CHECKS.append((name, bool(ok)))
    print(f"CHECK {name}: {'ok' if ok else 'FAIL'} {detail}")


def _background_name(slicer, widget_name):
    widget = slicer.app.layoutManager().sliceWidget(widget_name)
    node_id = widget.sliceLogic().GetSliceCompositeNode().GetBackgroundVolumeID()
    node = slicer.mrmlScene.GetNodeByID(node_id)
    return node.GetName() if node is not None else None


def main():
    import slicer
    from TargetingLib.logic import TargetingLogic

    videos = pathlib.Path(os.environ.get(
        "HIMX_TEST_VIDEOS", PROJECT_ROOT / "data" / "multiview-beef"))
    uv = os.environ.get("HIMX_TEST_UV") or shutil.which("uv")
    uv = uv or str(pathlib.Path.home() / ".local" / "bin" / "uv")
    check("the mock clip folder exists", videos.is_dir(), str(videos))
    check("uv is available", pathlib.Path(uv).is_file(), uv)
    if not videos.is_dir() or not pathlib.Path(uv).is_file():
        return _report()

    slicer.app.layoutManager().setLayout(
        slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
    logic = TargetingLogic()
    port = int(os.environ.get("HIMX_TEST_PORT", "18944"))

    # A failed socket bind must not be reported as an eternal "waiting".
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        logic.connect_camera_stream(port)
        deadline = time.time() + 2.0
        busy_status = logic.camera_stream_status()
        while time.time() < deadline and busy_status["status"] != "error":
            slicer.app.processEvents()
            time.sleep(0.1)
            busy_status = logic.camera_stream_status()
        check("a busy stream port is reported instead of waiting forever",
              busy_status["status"] == "error"
              and "port" in busy_status["message"].lower(),
              str(busy_status))
    finally:
        blocker.close()
        logic.disconnect_camera_stream()

    result = logic.connect_camera_stream(port)
    check("the OpenIGTLink listener starts",
          result["status"] in ("waiting", "connected"), str(result))
    if result["status"] not in ("waiting", "connected"):
        return _report()

    log_path = pathlib.Path("/tmp/opencode/camera_stream_smoke.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    process = None
    try:
        process = subprocess.Popen(
            [uv, "run", "tools/camera_stream.py", "--videos", str(videos),
             "--fps", "8", "--max-frames", "400", "--port", str(port)],
            cwd=str(PROJECT_ROOT), stdout=log, stderr=subprocess.STDOUT)
        status = result
        deadline = time.time() + 40
        while time.time() < deadline:
            slicer.app.processEvents()
            status = logic.camera_stream_status()
            if (status["status"] == "connected"
                    and len(status.get("views") or {}) == 3):
                break
            time.sleep(0.1)
        check("three camera volumes bind",
              status["status"] == "connected"
              and len(status.get("views") or {}) == 3,
              str(status))
        if status["status"] != "connected" or len(status.get("views") or {}) != 3:
            return _report()

        for view, name in zip(("Red", "Green", "Yellow"), ("c1", "c2", "c3")):
            found = _background_name(slicer, view)
            check(f"{view} shows {name}", found == name, f"found {found!r}")
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            image = node.GetImageData() if node is not None else None
            check(f"{name} is one 320x320 axial slice",
                  image is not None and image.GetDimensions() == (320, 320, 1),
                  str(image.GetDimensions() if image is not None else None))

        nodes = [slicer.mrmlScene.GetFirstNodeByName(name)
                 for name in ("c1", "c2", "c3")]
        before = [node.GetImageData().GetMTime() for node in nodes
                  if node is not None and node.GetImageData() is not None]
        for _ in range(30):
            slicer.app.processEvents()
            time.sleep(0.05)
        after = [node.GetImageData().GetMTime() for node in nodes
                 if node is not None and node.GetImageData() is not None]
        check("later frames update the same three nodes",
              len(before) == 3 and len(after) == 3
              and all(later > earlier for earlier, later in zip(before, after)),
              f"{before} -> {after}")
        check("the views stay bound after updates",
              [_background_name(slicer, view)
               for view in ("Red", "Green", "Yellow")] == ["c1", "c2", "c3"])

        deadline = time.time() + 20
        state = {}
        while time.time() < deadline:
            slicer.app.processEvents()
            status = logic.camera_stream_status()
            if status.get("frame_id") is not None:
                break
            time.sleep(0.05)
        check("the sender publishes a frame number",
              status.get("frame_id") is not None, str(status))

        logic.pause_camera_stream()
        deadline = time.time() + 20
        while time.time() < deadline:
            slicer.app.processEvents()
            status = logic.camera_stream_status()
            if status.get("paused") is True:
                break
            time.sleep(0.1)
        check("the pause command freezes the video", status.get("paused") is True,
              str(status))

        paused_frame = status.get("frame_id")
        logic.step_camera_stream()
        deadline = time.time() + 20
        while time.time() < deadline:
            slicer.app.processEvents()
            status = logic.camera_stream_status()
            if status.get("frame_id") != paused_frame and status.get("paused") is True:
                break
            time.sleep(0.1)
        check("step advances one frame and stays paused",
              status.get("frame_id") != paused_frame
              and status.get("paused") is True, str(status))

        for view in ("Red", "Green", "Yellow"):
            logic.set_camera_pixel(view, 160.0, 160.0)

        deadline = time.time() + 20
        result_status = logic.stream_result_status()
        while time.time() < deadline:
            slicer.app.processEvents()
            result_status = logic.stream_result_status()
            if result_status["status"] == "uncalibrated":
                break
            time.sleep(0.1)
        check("an uncalibrated video click set is refused clearly",
              result_status["status"] == "uncalibrated", str(result_status))
        check("the manually paused video stays paused for the refused set",
              logic.camera_stream_status().get("paused") is True,
              str(logic.camera_stream_status()))
        state_path = PROJECT_ROOT / "logs" / "camera-stream" / "stream-state.json"
        state = json.loads(state_path.read_text())
        check("the paused frame number is recorded",
              state.get("frame_id") is not None and state.get("paused") is True,
              str(state))
        result_path = PROJECT_ROOT / "logs" / "camera-stream" / "result.json"
        result_doc = json.loads(result_path.read_text())
        check("the uncalibrated result carries the clicked pixels",
              result_doc.get("pixels") and result_doc.get("calibrated") is False,
              str(result_doc)[:300])

        logic.clear_camera_clicks()
        deadline = time.time() + 20
        while time.time() < deadline:
            slicer.app.processEvents()
            if logic.camera_stream_status().get("paused") is False:
                break
            time.sleep(0.1)
        check("clearing clicks resumes the video",
              logic.camera_stream_status().get("paused") is False,
              str(logic.camera_stream_status()))
    except Exception:
        traceback.print_exc()
        check("the smoke ran without an exception", False)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                process.kill()
        log.close()
        logic.disconnect_camera_stream()
    print(f"SENDER LOG: {log_path}", flush=True)
    return _report()


def _report():
    passed = sum(1 for _, ok in _CHECKS if ok)
    total = len(_CHECKS)
    print(f"SMOKE {'PASS' if passed == total else 'FAIL'} {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
