"""Slicer smoke: synthetic clicks solve to the known marker at the origin.

    timeout 300 <slicer> --no-splash --python-script tools/smoke_synthetic_stream.py

This is Stage 2A's end-to-end proof: synthetic frames -> OpenIGTLink -> Slicer
clicks -> triangulation -> camera log -> 3D point. The scene's truth is the
origin, so the solved point is checked against a known answer, not against how
plausible it looks.
"""

import json
import os
import pathlib
import shutil
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


def main():
    import slicer
    from TargetingLib.logic import (
        CAMERA_CLICK_COLOURS,
        EXTERNAL_COLOUR,
        EXTERNAL_NODE_NAME,
        STREAM_FRUSTUM_NODE_NAMES,
        STREAM_ORIGIN_NODE_NAMES,
        STREAM_RAY_NODE_NAMES,
        TargetingLogic,
    )
    from targeting.rig import load_rig
    from targeting.triangulation import project, ray_for

    uv = os.environ.get("HIMX_TEST_UV") or shutil.which("uv")
    uv = uv or str(pathlib.Path.home() / ".local" / "bin" / "uv")
    check("uv is available", pathlib.Path(uv).is_file(), uv)
    if not pathlib.Path(uv).is_file():
        return _report()

    slicer.app.layoutManager().setLayout(
        slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
    logic = TargetingLogic()
    port = int(os.environ.get("HIMX_TEST_PORT", "18944"))
    result = logic.connect_camera_stream(port)
    check("the OpenIGTLink listener starts",
          result["status"] in ("waiting", "connected"), str(result))
    if result["status"] not in ("waiting", "connected"):
        return _report()

    log_path = pathlib.Path("/tmp/opencode/synthetic_stream_smoke.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    process = None
    try:
        process = subprocess.Popen(
            [uv, "run", "tools/synthetic_stream.py",
             "--fps", "5", "--max-frames", "400", "--port", str(port)],
            cwd=str(PROJECT_ROOT), stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        status = result
        while time.time() < deadline:
            slicer.app.processEvents()
            status = logic.camera_stream_status()
            if (status["status"] == "connected"
                    and len(status.get("views") or {}) == 3):
                break
            time.sleep(0.1)
        check("three synthetic volumes bind",
              status["status"] == "connected"
              and len(status.get("views") or {}) == 3,
              str(status))
        if status["status"] != "connected" or len(status.get("views") or {}) != 3:
            return _report()

        rig_path = PROJECT_ROOT / "logs" / "camera-stream" / "camera-rig.json"
        deadline = time.time() + 30
        rig = None
        while time.time() < deadline:
            try:
                rig = json.loads(rig_path.read_text())
                if len(rig.get("cameras", ())) == 3:
                    break
            except (OSError, ValueError):
                pass
            slicer.app.processEvents()
            time.sleep(0.1)
        check("the synthetic rig is published",
              rig is not None and len(rig.get("cameras", ())) == 3,
              str(rig_path))

        frusta = [slicer.mrmlScene.GetFirstNodeByName(name)
                  for name in STREAM_FRUSTUM_NODE_NAMES]
        origins = [slicer.mrmlScene.GetFirstNodeByName(name)
                   for name in STREAM_ORIGIN_NODE_NAMES]
        check("camera positions are shown before any click",
              all(node is not None for node in frusta + origins),
              f"frusta={[node is not None for node in frusta]} "
              f"origins={[node is not None for node in origins]}")
        try:
            scene_rig = load_rig(PROJECT_ROOT / "rig.json")
            wanted_origins = []
            for camera in rig["cameras"]:
                width = int(camera["width"])
                height = int(camera["height"])
                origin, _direction = ray_for(
                    camera["projection"], (0, 0))
                wanted_origins.append(tuple(
                    round(float(value), 2)
                    for value in scene_rig.to_ras(origin)))
            shown_origins = []
            for node in origins:
                position = [0.0, 0.0, 0.0]
                if node is not None:
                    node.GetNthControlPointPosition(0, position)
                shown_origins.append(tuple(round(float(v), 2)
                                           for v in position))
            check("camera origin points sit at the calibrated centres",
                  shown_origins == wanted_origins,
                  f"shown={shown_origins} want={wanted_origins}")
            labels = []
            for node in origins:
                labels.append(node.GetNthControlPointLabel(0)
                              if node is not None else None)
            check("camera origins are labelled C1, C2, C3",
                  labels == ["C1", "C2", "C3"], f"labels={labels}")
        except Exception as exc:  # noqa: BLE001 - evidence, not the test
            check("camera origin points sit at the calibrated centres",
                  False, str(exc))

        # The operator requires the video to keep playing during a drag. The
        # module may defer its own publishing until release, but it must never
        # pause, resume, or otherwise command the stream.
        probe = logic.probe_node()
        logic.watch_probe()
        before = logic.camera_stream_status()
        probe.InvokeEvent(probe.PointStartInteractionEvent)
        deadline = time.time() + 3.0
        during = before
        while time.time() < deadline:
            slicer.app.processEvents()
            during = logic.camera_stream_status()
            if during.get("frame_id") not in (None, before.get("frame_id")):
                break
            time.sleep(0.05)
        check("a point drag never pauses the live stream",
              during.get("paused") is False
              and during.get("status") == "connected",
              str(during))
        check("the live stream keeps advancing during a point drag",
              during.get("frame_id") not in (None, before.get("frame_id")),
              f"before={before.get('frame_id')} during={during.get('frame_id')}")
        probe.InvokeEvent(probe.PointEndInteractionEvent)

        clicks_ok = True
        click_frames = []
        if rig is not None:
            for index, (widget, camera) in enumerate(
                    zip(("Red", "Green", "Yellow"), rig["cameras"])):
                click_frames.append(
                    logic.camera_stream_status().get("frame_id"))
                u, v = project(camera["projection"], (0.0, 0.0, 0.0))
                try:
                    logic.set_camera_pixel(widget, float(u), float(v))
                except Exception as exc:  # noqa: BLE001 - reported below
                    clicks_ok = False
                    print(f"click {widget} failed: {exc}", flush=True)
                if index < 2:
                    # A live stream keeps advancing while the operator clicks
                    # the other two views; wait for it so the frame numbers
                    # prove it and the solve spans more than one frame.
                    deadline = time.time() + 3.0
                    now = click_frames[-1]
                    while time.time() < deadline:
                        slicer.app.processEvents()
                        now = logic.camera_stream_status().get("frame_id")
                        if now != click_frames[-1]:
                            break
                        time.sleep(0.05)
                    click_frames.append(now)
        check("all three marker-centre clicks publish", clicks_ok)
        check("the video advances between the three clicks",
              len({frame for frame in click_frames if frame is not None}) >= 2,
              f"frames={click_frames}")

        deadline = time.time() + 30
        result_status = logic.stream_result_status()
        while time.time() < deadline:
            slicer.app.processEvents()
            result_status = logic.stream_result_status()
            if result_status["status"] == "found":
                break
            time.sleep(0.1)
        check("the synthetic click set solves", result_status["status"] == "found",
              str(result_status))
        if result_status["status"] != "found":
            return _report()

        error = result_status.get("error_mm")
        residuals = result_status.get("residual_px") or []
        check("the solved point matches synthetic truth",
              error is not None and float(error) < 1.0,
              f"error={error}")
        check("the clicks reproject onto themselves",
              residuals and max(float(value) for value in residuals) < 1.0,
              f"residual_px={residuals}")

        external = slicer.mrmlScene.GetFirstNodeByName(EXTERNAL_NODE_NAME)
        check("the measured point is loaded in the scene",
              external is not None and external.GetNumberOfControlPoints() >= 1,
              f"points={external.GetNumberOfControlPoints() if external else None}")
        if external is not None and external.GetNumberOfControlPoints() >= 1:
            position = [0.0, 0.0, 0.0]
            external.GetNthControlPointPosition(
                external.GetNumberOfControlPoints() - 1, position)
            check("the loaded point is at the synthetic truth",
                  max(abs(value) for value in position) < 1.0,
                  f"RAS={tuple(round(value, 3) for value in position)}")

        rays = [slicer.mrmlScene.GetFirstNodeByName(name)
                for name in STREAM_RAY_NODE_NAMES]
        check("three camera rays are drawn",
              all(node is not None and node.GetNumberOfControlPoints() == 2
                  for node in rays),
              f"rays={[node.GetNumberOfControlPoints() if node else None for node in rays]}")

        def _selected_colour(node):
            display = node.GetDisplayNode() if node is not None else None
            try:
                return tuple(round(float(value), 3)
                             for value in display.GetSelectedColor())
            except AttributeError:
                return None

        wanted_ray_colours = [tuple(round(float(value), 3) for value in colour)
                              for colour in CAMERA_CLICK_COLOURS]
        ray_colours = [_selected_colour(node) for node in rays]
        check("camera rays keep their own colours when Slicer selects them",
              ray_colours == wanted_ray_colours,
              f"selected={ray_colours} want={wanted_ray_colours}")
        result_colour = _selected_colour(external)
        check("the measured point keeps its cyan colour when Slicer selects it",
              result_colour == tuple(round(float(value), 3)
                                     for value in EXTERNAL_COLOUR),
              f"selected={result_colour} want={EXTERNAL_COLOUR}")

        live = logic.camera_stream_status()
        check("the synthetic video keeps playing while clicks solve",
              live.get("status") == "connected"
              and live.get("paused") is False,
              str(live))
        frame_before = live.get("frame_id")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            slicer.app.processEvents()
            live = logic.camera_stream_status()
            if live.get("frame_id") != frame_before:
                break
            time.sleep(0.1)
        check("the synthetic video advances after the solve",
              live.get("frame_id") not in (None, frame_before),
              f"before={frame_before} after={live.get('frame_id')}")

        out = PROJECT_ROOT / "logs" / "synthetic-stream-smoke.png"
        try:
            slicer.util.mainWindow().grab().save(str(out))
            check("the 3D result is captured", out.is_file(), str(out))
        except Exception as exc:  # noqa: BLE001 - a screenshot is evidence, not the test
            check("the 3D result is captured", False, str(exc))
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
