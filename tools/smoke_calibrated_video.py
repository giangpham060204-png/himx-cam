"""Slicer smoke: a calibrated video click set solves end to end.

    timeout 300 <slicer> --no-splash --python-script tools/smoke_calibrated_video.py

The mock frames stand in for the cameras; the calibration fixture is the same
3x4 projection contract a real HM01B0 calibration will produce. The clicks are
placed at the fixture's marker-centre projections, so the test proves the
calibrated path: freeze -> click -> triangulate -> camera log -> rays ->
predicted pixels. It is not a claim that the mock content matches that rig.
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
    # Pre-fix names are still cleared so an old scene cannot leave them behind.
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
    from targeting.triangulation import project

    calibration_path = pathlib.Path(os.environ.get(
        "HIMX_TEST_CALIBRATION",
        PROJECT_ROOT / "tests" / "fixtures" / "synthetic-camera-rig.json"))
    videos = pathlib.Path(os.environ.get(
        "HIMX_TEST_VIDEOS", PROJECT_ROOT / "data" / "multiview-beef"))
    uv = os.environ.get("HIMX_TEST_UV") or shutil.which("uv")
    uv = uv or str(pathlib.Path.home() / ".local" / "bin" / "uv")
    check("the calibration fixture exists", calibration_path.is_file(),
          str(calibration_path))
    check("the mock clip folder exists", videos.is_dir(), str(videos))
    check("uv is available", pathlib.Path(uv).is_file(), uv)
    if not calibration_path.is_file() or not videos.is_dir() or not pathlib.Path(uv).is_file():
        return _report()

    calibration = json.loads(calibration_path.read_text())
    cameras = calibration.get("cameras") or []
    check("the fixture has three calibrated cameras", len(cameras) == 3,
          str(len(cameras)))
    if len(cameras) != 3:
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

    log_path = pathlib.Path("/tmp/opencode/calibrated_video_smoke.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    process = None
    try:
        process = subprocess.Popen(
            [uv, "run", "tools/camera_stream.py", "--videos", str(videos),
             "--calibration", str(calibration_path),
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
        check("three calibrated video volumes bind",
              status["status"] == "connected", str(status))
        check("the sender publishes a frame number",
              status.get("frame_id") is not None, str(status))
        if status["status"] != "connected" or status.get("frame_id") is None:
            return _report()

        furniture = []
        for name in ("CameraStreamFrustumC1", "CameraStreamOriginC1",
                     "CameraStreamFrustumC2", "CameraStreamOriginC2",
                     "CameraStreamFrustumC3", "CameraStreamOriginC3"):
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            display = node.GetDisplayNode() if node is not None else None
            if display is None:
                furniture.append(False)
                continue
            getter = getattr(display, "GetPickable", None)
            if getter is None:
                getter = getattr(display, "GetSelectable", None)
            furniture.append(bool(getter is not None and not getter()))
        check("camera furniture cannot steal a 3D-view drag",
              all(furniture), str(furniture))

        logic.pause_camera_stream()
        deadline = time.time() + 20
        while time.time() < deadline:
            slicer.app.processEvents()
            status = logic.camera_stream_status()
            if status.get("paused") is True:
                break
            time.sleep(0.1)
        check("the pause command freezes the calibrated video",
              status.get("paused") is True, str(status))

        for index, (widget, camera) in enumerate(
                zip(("Red", "Green", "Yellow"), cameras)):
            u_px, v_px = project(camera["projection"], (0.0, 0.0, 0.0))
            logic.set_camera_pixel(widget, float(u_px), float(v_px))
            if index == 0:
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
                ray_views = (ray.GetDisplayNode().GetViewNodeIDs()
                             if ray is not None and ray.GetDisplayNode() else ())
                check(
                    "the 3D ray is restricted to the 3D view",
                    len(ray_views) == 1 and "View" in ray_views[0],
                    str(ray_views),
                )
                check(
                    "the first click draws an epipolar line in c2 and c3",
                    all(node is not None and node.GetNumberOfControlPoints() == 2
                        for node in (epipolar_c2, epipolar_c3)),
                    f"c2={epipolar_c2.GetNumberOfControlPoints() if epipolar_c2 else None} "
                    f"c3={epipolar_c3.GetNumberOfControlPoints() if epipolar_c3 else None}",
                )
                check(
                    "one click does not invent a 3D point",
                    logic.stream_result_status()["status"] != "found",
                    str(logic.stream_result_status()),
                )
                guide_image = PROJECT_ROOT / "logs" / "ray-guide-smoke.png"
                try:
                    slicer.app.processEvents()
                    slicer.util.mainWindow().grab().save(str(guide_image))
                    check("the first-click guide is captured",
                          guide_image.is_file(), str(guide_image))
                except Exception as exc:  # noqa: BLE001 - evidence, not the test
                    check("the first-click guide is captured", False, str(exc))

        guide_rays = [slicer.mrmlScene.GetFirstNodeByName(guide_ray_name(index))
                      for index in (1, 2, 3)]
        guide_lines = [slicer.mrmlScene.GetFirstNodeByName(
            guide_line_name(source, target))
            for source in (1, 2, 3) for target in (1, 2, 3)
            if source != target]
        check("each clicked camera keeps its own 3D ray",
              all(guide_point_count(node) == 2 for node in guide_rays),
              f"rays={[guide_point_count(node) for node in guide_rays]}")
        check("each clicked camera draws epipolar lines in the other views",
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
        check("the calibrated click set solves", result_status["status"] == "found",
              str(result_status))
        if result_status["status"] != "found":
            return _report()

        guide_names = all_guide_names()

        def guide_visible(name):
            node = slicer.mrmlScene.GetFirstNodeByName(name)
            display = node.GetDisplayNode() if node is not None else None
            return bool(display.GetVisibility()) if display is not None else False

        check(
            "the solved result hides the click-ray guide",
            not any(guide_visible(name) for name in guide_names),
            str([name for name in guide_names if guide_visible(name)]),
        )

        residuals = result_status.get("residual_px") or []
        check("the calibrated clicks reproject onto themselves",
              residuals and max(float(value) for value in residuals) < 1.0,
              f"residual_px={residuals}")
        check("the result is marked as calibrated video",
              result_status["result"].get("calibrated") is True
              and result_status["result"].get("source") == "video",
              str(result_status["result"])[:300])

        external = slicer.mrmlScene.GetFirstNodeByName("ExternalTargets")
        check("the measured point is loaded in the scene",
              external is not None and external.GetNumberOfControlPoints() >= 1,
              f"points={external.GetNumberOfControlPoints() if external else None}")
        check("the live result shows one current point, not the history",
              external is not None and external.GetNumberOfControlPoints() == 1,
              f"points={external.GetNumberOfControlPoints() if external else None}")
        if external is not None and external.GetNumberOfControlPoints() >= 1:
            position = [0.0, 0.0, 0.0]
            external.GetNthControlPointPosition(
                external.GetNumberOfControlPoints() - 1, position)
            check("the fixture's point is at the origin",
                  max(abs(value) for value in position) < 1.0,
                  f"RAS={tuple(round(value, 3) for value in position)}")
        tip = slicer.mrmlScene.GetFirstNodeByName("VirtualTip")
        external_colour = (external.GetDisplayNode().GetColor()
                           if external is not None and external.GetDisplayNode()
                           else None)
        tip_colour = (tip.GetDisplayNode().GetColor()
                      if tip is not None and tip.GetDisplayNode() else None)
        check("the measured point uses a distinct cyan colour",
              is_cyan(external) and is_cyan(tip),
              f"external={external_colour} tip={tip_colour}")

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
        check("result rays hide their length labels",
              all(node is not None and node.GetDisplayNode() is not None
                  and not node.GetDisplayNode().GetPropertiesLabelVisibility()
                  for node in rays),
              str([node.GetDisplayNode().GetPropertiesLabelVisibility()
                   if node is not None and node.GetDisplayNode() else None
                   for node in rays]))

        out = PROJECT_ROOT / "logs" / "calibrated-video-smoke.png"
        try:
            slicer.app.processEvents()
            slicer.util.mainWindow().grab().save(str(out))
            check("the 3D result is captured", out.is_file(), str(out))
        except Exception as exc:  # noqa: BLE001 - evidence, not the test
            check("the 3D result is captured", False, str(exc))

        logic.clear_camera_clicks()
        deadline = time.time() + 20
        while time.time() < deadline:
            slicer.app.processEvents()
            if logic.camera_stream_status().get("paused") is False:
                break
            time.sleep(0.1)
        check("clearing clicks resumes the calibrated video",
              logic.camera_stream_status().get("paused") is False,
              str(logic.camera_stream_status()))
        check("clearing clicks removes the solved rays",
              all(slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamRayC{index}") is None
                  for index in (1, 2, 3)),
              str([f"CameraStreamRayC{index}"
                   for index in (1, 2, 3)
                   if slicer.mrmlScene.GetFirstNodeByName(
                       f"CameraStreamRayC{index}") is not None]))
        check("clearing clicks removes the predicted pixels",
              all(slicer.mrmlScene.GetFirstNodeByName(
                  f"CameraStreamPredictedC{index}") is None
                  for index in (1, 2, 3)),
              str([f"CameraStreamPredictedC{index}"
                   for index in (1, 2, 3)
                   if slicer.mrmlScene.GetFirstNodeByName(
                       f"CameraStreamPredictedC{index}") is not None]))
        cleared_external = slicer.mrmlScene.GetFirstNodeByName("ExternalTargets")
        check("clearing clicks removes the 3D result point",
              cleared_external is None
              or cleared_external.GetNumberOfControlPoints() == 0,
              f"points={cleared_external.GetNumberOfControlPoints() if cleared_external else 0}")
        check("clearing clicks removes the virtual tip",
              slicer.mrmlScene.GetFirstNodeByName("VirtualTip") is None,
              str(slicer.mrmlScene.GetFirstNodeByName("VirtualTip")))
        check("clearing clicks resets the result status",
              logic.stream_result_status()["status"] == "none",
              str(logic.stream_result_status())[:300])
        still_there = [slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamFrustumC{index}")
                       for index in (1, 2, 3)]
        still_there += [slicer.mrmlScene.GetFirstNodeByName(f"CameraStreamOriginC{index}")
                        for index in (1, 2, 3)]
        check("clearing clicks keeps the camera positions in the 3D view",
              all(node is not None for node in still_there),
              str([node.GetName() if node else None for node in still_there]))
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
