"""Slicer smoke: synthetic MP4s solve against a known point.

    timeout 300 <slicer> --no-splash --python-script tools/smoke_synthetic_video.py

The generator writes three pinhole MP4s, their synthetic calibration, the truth
point, and the marker centres. The normal video sender then streams them with
``--truth``; Slicer pauses, clicks the centres, and the panel must report a real
``error_mm``. This is the video-path equivalent of the synthetic truth smoke.
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


def guide_point_count(node):
    """Points in either the guide's model ray or a markups line."""
    if node is None:
        return None
    if node.IsA("vtkMRMLModelNode"):
        data = node.GetPolyData()
        return data.GetNumberOfPoints() if data is not None else None
    if node.IsA("vtkMRMLMarkupsLineNode"):
        return node.GetNumberOfControlPoints()
    return None


def guide_ray_name(index):
    return f"CameraStreamClickRayC{index}"


def guide_line_name(source, target):
    return f"CameraStreamEpipolarC{target}FromC{source}"


def all_guide_names():
    names = [guide_ray_name(index) for index in (1, 2, 3)]
    names.extend(guide_line_name(source, target)
                 for source in (1, 2, 3) for target in (1, 2, 3)
                 if source != target)
    names.extend(("CameraStreamClickRay", "CameraStreamEpipolarC1",
                  "CameraStreamEpipolarC2", "CameraStreamEpipolarC3"))
    return tuple(names)


def is_cyan(node):
    if node is None or node.GetDisplayNode() is None:
        return False
    display = node.GetDisplayNode()
    red, green, blue = display.GetColor()
    if not (red < 0.25 and green > 0.7 and blue > 0.8):
        return False
    try:
        selected_red, selected_green, selected_blue = display.GetSelectedColor()
    except AttributeError:
        return True
    return (selected_red < 0.25 and selected_green > 0.7
            and selected_blue > 0.8)


def main():
    import slicer
    from TargetingLib.logic import TargetingLogic

    fixture = PROJECT_ROOT / "logs" / "synthetic-video-smoke"
    uv = os.environ.get("HIMX_TEST_UV") or shutil.which("uv")
    uv = uv or str(pathlib.Path.home() / ".local" / "bin" / "uv")
    check("uv is available", pathlib.Path(uv).is_file(), uv)
    if not pathlib.Path(uv).is_file():
        return _report()

    generator_log = pathlib.Path("/tmp/opencode/synthetic_video_generator.log")
    generator_log.parent.mkdir(parents=True, exist_ok=True)
    with generator_log.open("w", encoding="utf-8") as log:
        generated = subprocess.run(
            [uv, "run", "tools/synthetic_videos.py", "--out", str(fixture),
             "--frames", "3", "--fps", "10"],
            cwd=str(PROJECT_ROOT), stdout=log, stderr=subprocess.STDOUT)
    check("the synthetic video generator succeeds", generated.returncode == 0,
          f"exit={generated.returncode}; log={generator_log}")
    if generated.returncode != 0:
        return _report()
    rig = fixture / "camera-rig.json"
    truth_path = fixture / "truth.json"
    clicks_path = fixture / "marker-centres.json"
    check("the generator writes the rig and truth", rig.is_file() and truth_path.is_file(),
          f"rig={rig} truth={truth_path}")
    if not (rig.is_file() and truth_path.is_file() and clicks_path.is_file()):
        return _report()
    truth = json.loads(truth_path.read_text())
    clicks = json.loads(clicks_path.read_text())
    check("the generator records three marker centres",
          len(clicks.get("pixels") or []) == 3, str(clicks)[:300])

    slicer.app.layoutManager().setLayout(
        slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
    logic = TargetingLogic()
    port = int(os.environ.get("HIMX_TEST_PORT", "18944"))
    result = logic.connect_camera_stream(port)
    check("the OpenIGTLink listener starts",
          result["status"] in ("waiting", "connected"), str(result))
    if result["status"] not in ("waiting", "connected"):
        return _report()

    stream_log = pathlib.Path("/tmp/opencode/synthetic_video_stream.log")
    log = stream_log.open("w", encoding="utf-8")
    process = None
    try:
        process = subprocess.Popen(
            [uv, "run", "tools/camera_stream.py", "--videos", str(fixture),
             "--calibration", str(rig), "--truth", str(truth_path),
             "--fps", "8", "--max-frames", "400", "--port", str(port)],
            cwd=str(PROJECT_ROOT), stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 40
        status = result
        while time.time() < deadline:
            slicer.app.processEvents()
            status = logic.camera_stream_status()
            if status["status"] == "connected" and status.get("frame_id") is not None:
                break
            time.sleep(0.1)
        check("three synthetic-video volumes bind",
              status["status"] == "connected", str(status))
        check("the sender publishes a frame number",
              status.get("frame_id") is not None, str(status))
        if status["status"] != "connected" or status.get("frame_id") is None:
            return _report()

        logic.pause_camera_stream()
        deadline = time.time() + 20
        while time.time() < deadline:
            slicer.app.processEvents()
            status = logic.camera_stream_status()
            if status.get("paused") is True:
                break
            time.sleep(0.1)
        check("the pause command freezes the synthetic video",
              status.get("paused") is True, str(status))

        for index, (view, pixel) in enumerate(
                zip(("Red", "Green", "Yellow"), clicks["pixels"])):
            logic.set_camera_pixel(view, float(pixel[0]), float(pixel[1]))
            if index == 0:
                slicer.app.processEvents()
                ray = slicer.mrmlScene.GetFirstNodeByName(guide_ray_name(1))
                epipolar_c2 = slicer.mrmlScene.GetFirstNodeByName(
                    guide_line_name(1, 2))
                epipolar_c3 = slicer.mrmlScene.GetFirstNodeByName(
                    guide_line_name(1, 3))
                check(
                    "the first click draws the 3D ray",
                    guide_point_count(ray) == 2,
                    f"points={guide_point_count(ray)}",
                )
                check(
                    "the 3D ray is a model line hidden from slice views",
                    ray is not None and ray.IsA("vtkMRMLModelNode")
                    and ray.GetDisplayNode().GetVisibility2D() == 0,
                    f"type={ray.GetClassName() if ray else None}",
                )
                check(
                    "one synthetic click draws epipolar lines in c2 and c3",
                    all(node is not None and node.GetNumberOfControlPoints() == 2
                        for node in (epipolar_c2, epipolar_c3)),
                    f"c2={epipolar_c2.GetNumberOfControlPoints() if epipolar_c2 else None} "
                    f"c3={epipolar_c3.GetNumberOfControlPoints() if epipolar_c3 else None}",
                )
                guide_image = PROJECT_ROOT / "logs" / "ray-guide-synthetic.png"
                slicer.util.mainWindow().grab().save(str(guide_image))
                check("the synthetic first-click guide is captured",
                      guide_image.is_file(), str(guide_image))

        guide_rays = [slicer.mrmlScene.GetFirstNodeByName(guide_ray_name(index))
                      for index in (1, 2, 3)]
        guide_lines = [slicer.mrmlScene.GetFirstNodeByName(
            guide_line_name(source, target))
            for source in (1, 2, 3) for target in (1, 2, 3)
            if source != target]
        check("each synthetic click keeps its own 3D ray",
              all(guide_point_count(node) == 2 for node in guide_rays),
              f"rays={[guide_point_count(node) for node in guide_rays]}")
        check("each synthetic click draws epipolar lines in the other views",
              all(node is not None and node.GetNumberOfControlPoints() == 2
                  for node in guide_lines),
              f"lines={[node.GetNumberOfControlPoints() if node else None
                        for node in guide_lines]}")

        deadline = time.time() + 30
        result_status = logic.stream_result_status()
        while time.time() < deadline:
            slicer.app.processEvents()
            result_status = logic.stream_result_status()
            if result_status["status"] == "found":
                break
            time.sleep(0.1)
        check("the synthetic-video click set solves",
              result_status["status"] == "found", str(result_status))
        if result_status["status"] != "found":
            return _report()
        error = result_status.get("error_mm")
        check("the result has a real truth error",
              error is not None and float(error) < 2.0, f"error_mm={error}")
        payload = result_status["result"]
        check("the result carries the video truth",
              payload.get("source") == "video"
              and payload.get("truth_mm") == truth.get("point_mm"),
              str(payload)[:300])

        external = slicer.mrmlScene.GetFirstNodeByName("ExternalTargets")
        check("the measured point is loaded in the scene",
              external is not None and external.GetNumberOfControlPoints() >= 1,
              f"points={external.GetNumberOfControlPoints() if external else None}")
        if external is not None and external.GetNumberOfControlPoints() >= 1:
            position = [0.0, 0.0, 0.0]
            external.GetNthControlPointPosition(
                external.GetNumberOfControlPoints() - 1, position)
            check("the synthetic point is at the origin",
                  max(abs(value) for value in position) < 2.0,
                  f"RAS={tuple(round(value, 3) for value in position)}")
        tip = slicer.mrmlScene.GetFirstNodeByName("VirtualTip")
        check("the synthetic result point is cyan and distinct from the rays",
              is_cyan(external) and is_cyan(tip),
              f"external={external.GetDisplayNode().GetColor() if external and external.GetDisplayNode() else None} "
              f"tip={tip.GetDisplayNode().GetColor() if tip and tip.GetDisplayNode() else None}")

        rays = [slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamRayC{index}")
                for index in (1, 2, 3)]
        frusta = [slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamFrustumC{index}")
                  for index in (1, 2, 3)]
        predicted = [slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamPredictedC{index}")
                     for index in (1, 2, 3)]
        check("three camera rays are drawn",
              all(node is not None and node.GetNumberOfControlPoints() == 2
                  for node in rays),
              f"rays={[node.GetNumberOfControlPoints() if node else None for node in rays]}")
        check("three camera frusta are drawn", all(node is not None for node in frusta))
        check("three predicted pixels are drawn",
              all(node is not None and node.GetNumberOfControlPoints() == 1
                  for node in predicted),
              f"predicted={[node.GetNumberOfControlPoints() if node else None for node in predicted]}")

        out = PROJECT_ROOT / "logs" / "synthetic-video-smoke.png"
        try:
            slicer.util.mainWindow().grab().save(str(out))
            check("the synthetic-video result is captured", out.is_file(), str(out))
        except Exception as exc:  # noqa: BLE001 - evidence, not the test
            check("the synthetic-video result is captured", False, str(exc))

        logic.clear_camera_clicks()
        deadline = time.time() + 20
        while time.time() < deadline:
            slicer.app.processEvents()
            if logic.camera_stream_status().get("paused") is False:
                break
            time.sleep(0.1)
        check("clearing clicks resumes the synthetic video",
              logic.camera_stream_status().get("paused") is False,
              str(logic.camera_stream_status()))

        # A deliberately inconsistent set must be refused, not turned into a
        # 20-40 mm point. The latest guide stays visible so the bad click can
        # be corrected against the epipolar lines.
        bad_pixels = [list(pixel) for pixel in clicks["pixels"]]
        bad_pixels[1] = [bad_pixels[1][0] + 20.0, bad_pixels[1][1] - 20.0]
        for view, pixel in zip(("Red", "Green", "Yellow"), bad_pixels):
            logic.set_camera_pixel(view, float(pixel[0]), float(pixel[1]))

        deadline = time.time() + 30
        refused = logic.stream_result_status()
        while time.time() < deadline:
            slicer.app.processEvents()
            refused = logic.stream_result_status()
            if refused["status"] == "inconsistent":
                break
            time.sleep(0.1)
        check("an inconsistent click set is refused, not solved",
              refused["status"] == "inconsistent"
              and "no point" in refused["message"],
              str(refused)[:400])
        guide_names = (
            [guide_ray_name(index) for index in (1, 2, 3)]
            + [guide_line_name(source, target)
               for source in (1, 2, 3) for target in (1, 2, 3)
               if source != target]
        )
        def visible(name):
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            display = node.GetDisplayNode() if node is not None else None
            return bool(display.GetVisibility()) if display is not None else False

        check("a refused set keeps all three click guides visible",
              all(visible(name) for name in guide_names),
              str([name for name in guide_names if not visible(name)]))
        check("a refused set hides the solved rays",
              not any(visible(f"CameraStreamRayC{index}") for index in (1, 2, 3)),
              str([f"CameraStreamRayC{index}" for index in (1, 2, 3)
                   if visible(f"CameraStreamRayC{index}")]))
        refused_external = slicer.mrmlScene.GetFirstNodeByName("ExternalTargets")
        check("a refused set hides the 3D result point",
              refused_external is None
              or refused_external.GetNumberOfControlPoints() == 0
              or not visible("ExternalTargets"),
              f"points={refused_external.GetNumberOfControlPoints() if refused_external else 0}")
        check("a refused set hides the virtual tip",
              not visible("VirtualTip"),
              str(slicer.mrmlScene.GetFirstNodeByName("VirtualTip")))
        still_there = [slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamFrustumC{index}")
                       for index in (1, 2, 3)]
        still_there += [slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamOriginC{index}")
                        for index in (1, 2, 3)]
        check("a refused set keeps the camera positions in the 3D view",
              all(node is not None for node in still_there),
              str([node.GetName() if node else None for node in still_there]))
        try:
            slicer.app.processEvents()
            refused_image = PROJECT_ROOT / "logs" / "ray-guide-inconsistent.png"
            slicer.util.mainWindow().grab().save(str(refused_image))
            check("the refused-set guide is captured",
                  refused_image.is_file(), str(refused_image))
        except Exception as exc:  # noqa: BLE001 - evidence, not the test
            check("the refused-set guide is captured", False, str(exc))
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
    print(f"GENERATOR LOG: {generator_log}", flush=True)
    print(f"STREAM LOG: {stream_log}", flush=True)
    return _report()


def _report():
    passed = sum(1 for _, ok in _CHECKS if ok)
    total = len(_CHECKS)
    print(f"SMOKE {'PASS' if passed == total else 'FAIL'} {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
