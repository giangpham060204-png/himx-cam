"""In-Slicer smoke test for the Targeting module.

Runs in a separate Slicer window:

    timeout 300 <slicer> \
        --no-splash --python-script tools/smoke_targeting_module.py

Loads the module directly (no installation needed), injects synthetic Qt
clicks and VTK mouse moves, and checks the click path. Prints one line per
check and finally "SMOKE PASS n/n" or "SMOKE FAIL: names".
"""

import pathlib
import sys
import tempfile
import traceback

# No hardcoded fallback: panic instead of guessing (see make_field_cube).


def _resolve_project_root(_globals=None, _env=None):
    """Checkout root without scanning the disk (same rule as make_field_cube)."""
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
        "to the directory containing frame.json, or set PROJECT_ROOT_OVERRIDE "
        f"in the Slicer console before exec(). __file__={here!r}"
    )


PROJECT_ROOT = _resolve_project_root()
sys.path.insert(0, str(PROJECT_ROOT / "SlicerTargeting" / "Targeting"))
sys.path.insert(0, str(PROJECT_ROOT))

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok), detail))
    print(f"CHECK {name}: {'ok' if ok else 'FAIL'} {detail}")


def main():
    try:
        import qt
        import slicer

        import Targeting as targeting_module
        from TargetingLib.logic import _LEFT_BUTTON, _MOUSE_PRESS

        no_modifier = getattr(qt.Qt, "NoModifier", None)
        if no_modifier is None:
            no_modifier = qt.Qt.KeyboardModifier.NoModifier

        ctrl = (getattr(qt.Qt, "ControlModifier", None)
                or qt.Qt.KeyboardModifier.ControlModifier)
        no_button = (getattr(qt.Qt, "NoButton", None)
                     or qt.Qt.MouseButton.NoButton)
        mouse_move = (getattr(qt.QEvent, "MouseMove", None)
                      or qt.QEvent.Type.MouseMove)
        mouse_release = (getattr(qt.QEvent, "MouseButtonRelease", None)
                         or qt.QEvent.Type.MouseButtonRelease)

        def make_mouse(kind, x, y, button, buttons, modifiers):
            point = qt.QPointF(float(x), float(y))
            return qt.QMouseEvent(kind, point, button, buttons, modifiers)

        def make_press(x, y):
            return make_mouse(
                _MOUSE_PRESS, x, y, _LEFT_BUTTON, _LEFT_BUTTON, no_modifier)

        from TargetingLib import logic as logic_module

        smoke_logs = pathlib.Path("/tmp/opencode/smoke_logs")
        logic_module.LOGS_DIR = smoke_logs
        if smoke_logs.exists():
            for stale in smoke_logs.glob("*.csv"):
                stale.unlink()

        import slicer_bridge.live_frame as bridge
        from targeting.converter import convert, cube_to_ras

        # Build the widget against a plain panel (the module manager is not
        # involved so this needs no installation). A QVBoxLayout mirrors the
        # real module panel, whose template calls layout.addStretch(1).
        panel = qt.QWidget()
        panel.setLayout(qt.QVBoxLayout())
        widget = targeting_module.TargetingWidget(panel)
        widget.setup()
        widget.enter()

        advanced = getattr(widget, "advancedPanel", None)
        check(
            "Advanced is collapsed and holds the legacy modes",
            advanced is not None
            and advanced.collapsed
            and advanced.isAncestorOf(widget.pickBox)
            and advanced.isAncestorOf(widget.probeBox),
            f"collapsed={None if advanced is None else advanced.collapsed}",
        )

        check(
            "the panel has Start/Stop video and a video status",
            all(hasattr(widget, name) for name in
                ("startVideoButton", "stopVideoButton", "videoLabel"))
            and widget.videoLabel.text.startswith("video:"),
            f"label={getattr(widget, 'videoLabel', None)}",
        )

        logic = widget.logic
        with tempfile.TemporaryDirectory(prefix="video-start-") as folder:
            clips = []
            for index in range(3):
                path = pathlib.Path(folder) / f"c{index}.mp4"
                path.write_bytes(b"")
                clips.append(path)

            class FakeVideoProcess:
                last = None

                def __init__(self, command, *, cwd, log_path):
                    self.command = [str(part) for part in command]
                    self.cwd = cwd
                    self.log_path = log_path
                    self.running = True
                    FakeVideoProcess.last = self

                def _status(self, status, message):
                    return {
                        "status": status,
                        "message": message,
                        "pid": 1 if status == "running" else None,
                        "returncode": None,
                        "log_path": str(self.log_path),
                        "log_tail": "",
                    }

                def start(self):
                    return self._status("running", "video: running (pid 1)")

                def poll(self):
                    return self._status(
                        "running" if self.running else "stopped",
                        "video: running (pid 1)" if self.running
                        else "video: stopped")

                def stop(self):
                    self.running = False
                    return self._status("stopped", "video: stopped")

            original = logic_module.VideoProcess
            logic_module.VideoProcess = FakeVideoProcess
            try:
                started = logic.start_video(clips, None, None, 8.0)
                command = FakeVideoProcess.last.command
                check(
                    "start_video builds the external sender command",
                    "--video-files" in command
                    and command[command.index("--video-files") + 1:
                                command.index("--video-files") + 4]
                    == [str(path) for path in clips]
                    and "--fps" in command,
                    " ".join(command),
                )
                check("start_video reports the sender running",
                      started.get("status") == "running", str(started))
                check("video_status follows the sender",
                      logic.video_status().get("status") == "running",
                      str(logic.video_status()))
                first = FakeVideoProcess.last
                logic.start_video(clips, None, None, 8.0)
                check("a second start replaces the first sender",
                      not first.running
                      and logic.video_status().get("status") == "running",
                      f"first_running={first.running}")
                stopped = logic.stop_video()
                check("stop_video stops the sender",
                      stopped.get("status") == "stopped", str(stopped))
            finally:
                logic_module.VideoProcess = original
                logic._video = None

        check(
            "video settings live inline in the main panel",
            all(hasattr(widget, name) for name in
                ("videoSettings", "c1Edit", "c2Edit", "c3Edit",
                 "calibrationEdit", "truthEdit", "fpsSpin", "portSpin",
                 "videoNote"))
            and not advanced.isAncestorOf(widget.videoSettings),
            str(getattr(widget, "videoSettings", None)),
        )
        widget._preset_synthetic_video()
        check(
            "the synthetic preset fills calibration and truth",
            pathlib.Path(widget.calibrationEdit.text).is_file()
            and pathlib.Path(widget.truthEdit.text).is_file(),
            f"calibration={widget.calibrationEdit.text!r} "
            f"truth={widget.truthEdit.text!r}",
        )
        widget._preset_beef_video()
        check(
            "the beef preset fills the inline demo calibration",
            pathlib.Path(widget.calibrationEdit.text).is_file()
            and "DEMO" in widget.videoNote.text,
            f"calibration={widget.calibrationEdit.text!r} "
            f"note={widget.videoNote.text!r}",
        )
        widget._preset_downloaded_video()

        layout = slicer.app.layoutManager()
        layout.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        slicer.app.processEvents()
        red_widget = layout.sliceWidget("Red")
        red_view = red_widget.sliceView()
        width, height = int(red_view.width), int(red_view.height)
        centre_x, centre_y = width // 2, height // 2

        view_filter = widget.filters["Red"][1]
        view_target = widget.filters["Red"][0]
        check(
            "the click filter sits on the widget that receives mouse events",
            "ctkVTKOpenGLNativeWidget" in type(view_target).__name__,
            f"target={type(view_target).__name__}",
        )

        # 1. disabled filter passes through
        passed = view_filter.eventFilter(red_view, make_press(centre_x, centre_y))
        check("filter passes through when disabled", passed is False)

        # 2. pick mode on: a delivered click places a target whose position
        #    equals Slicer's own reading of the same pixel
        widget.pickBox.checked = True
        click_x, click_y = centre_x + 15, centre_y - 10

        iren = red_view.renderWindow().GetInteractor()
        iren.SetEventPosition(click_x, height - 1 - click_y)  # device y is bottom-up
        iren.MouseMoveEvent()
        slicer.app.processEvents()
        crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        truth_ras = [0.0, 0.0, 0.0]
        crosshair.GetCursorPositionRAS(truth_ras)

        offset_before = list(red_widget.sliceLogic().GetSliceNode().GetXYZOrigin())
        qt.QApplication.sendEvent(view_target, make_press(click_x, click_y))
        slicer.app.processEvents()
        offset_after = list(red_widget.sliceLogic().GetSliceNode().GetXYZOrigin())

        check(
            "click detected with view and pixel",
            widget.lastClick == ("XY", float(click_x), float(click_y)),
            f"lastClick={widget.lastClick}",
        )
        check("consumed click did not move the view", offset_before == offset_after)

        tip = slicer.mrmlScene.GetFirstNodeByName("VirtualTip")
        tip_center = None
        if tip is not None and tip.GetPolyData() is not None:
            bounds = tip.GetPolyData().GetBounds()
            tip_center = [
                (bounds[0] + bounds[1]) / 2.0,
                (bounds[2] + bounds[3]) / 2.0,
                (bounds[4] + bounds[5]) / 2.0,
            ]
        tip_error = (
            max(abs(tip_center[i] - truth_ras[i]) for i in range(3))
            if tip_center
            else None
        )
        check(
            "virtual tip matches Slicer hit-testing",
            tip_error is not None and tip_error < 0.1,
            f"error={tip_error}",
        )

        targets = slicer.mrmlScene.GetFirstNodeByName("Targets")
        label = (
            targets.GetNthControlPointLabel(0)
            if targets and targets.GetNumberOfControlPoints()
            else None
        )
        point = (
            targets.GetNthControlPointPosition(0)
            if targets and targets.GetNumberOfControlPoints()
            else None
        )
        point_error = (
            max(abs(point[i] - truth_ras[i]) for i in range(3)) if point else None
        )
        check(
            "targets list holds T1 at the same point",
            label == "T1" and point_error is not None and point_error < 0.1,
            f"label={label} error={point_error}",
        )

        # 2b. a click far outside the field must be refused, loudly
        placed_before = widget.logic.placed
        rejected_before = widget.logic.rejected
        points_before = targets.GetNumberOfControlPoints()
        far_x = width - 5
        far_y = height - 5
        qt.QApplication.sendEvent(view_target, make_press(far_x, far_y))
        slicer.app.processEvents()
        check(
            "out-of-field click refused",
            widget.logic.placed == placed_before
            and widget.logic.rejected == rejected_before + 1
            and targets.GetNumberOfControlPoints() == points_before,
            f"placed={widget.logic.placed} rejected={widget.logic.rejected}",
        )
        check(
            "panel explains the refusal",
            "Outside the field" in widget.statusLabel.text,
            widget.statusLabel.text,
        )

        # 2c. the accepted click above wrote exactly one CSV row
        csv_files = sorted(smoke_logs.glob("targets-*.csv"))
        csv_ok = False
        csv_detail = f"files={[p.name for p in csv_files]}"
        if len(csv_files) == 1:
            lines = csv_files[0].read_text().splitlines()
            cube_expected = convert("XY", click_x, click_y, 0.0, bridge.live_frame())
            ras_expected = cube_to_ras(cube_expected, bridge.live_frame())
            row = lines[1].split(",") if len(lines) >= 2 else []
            csv_ok = (
                len(lines) == 2
                and row[1] == "XY"
                and max(
                    abs(float(row[i]) - ras_expected[i - 8]) for i in range(8, 11)
                )
                < 0.01
            )
            csv_detail += f" lines={len(lines)}"
        check("accepted click wrote one CSV row", csv_ok, csv_detail)

        # 2d. a mid-pipeline failure must leave the scene untouched
        points_before = targets.GetNumberOfControlPoints()
        placed_before = widget.logic.placed
        original_move_tip = widget.logic.move_tip

        def failing_move_tip(_ras):
            raise RuntimeError("injected failure")

        widget.logic.move_tip = failing_move_tip
        qt.QApplication.sendEvent(view_target, make_press(centre_x + 10, centre_y - 5))
        slicer.app.processEvents()
        widget.logic.move_tip = original_move_tip
        check(
            "failed pipeline leaves no orphan point",
            targets.GetNumberOfControlPoints() == points_before
            and widget.logic.placed == placed_before
            and "Internal error" in widget.statusLabel.text,
            f"points={targets.GetNumberOfControlPoints()} placed={widget.logic.placed}",
        )

        # 2e. an unwritable log location: target placed, panel warns
        blocked = pathlib.Path("/tmp/opencode/smoke_logs_blocked")
        if blocked.exists():
            blocked.unlink()
        blocked.write_text("x")
        logic_module.LOGS_DIR = blocked
        points_before = targets.GetNumberOfControlPoints()
        placed_before = widget.logic.placed
        qt.QApplication.sendEvent(view_target, make_press(centre_x + 12, centre_y + 8))
        slicer.app.processEvents()
        check(
            "unwritable log: placed with warning",
            widget.logic.placed == placed_before + 1
            and targets.GetNumberOfControlPoints() == points_before + 1
            and "log write failed" in widget.statusLabel.text,
            f"placed={widget.logic.placed} status={widget.statusLabel.text}",
        )
        logic_module.LOGS_DIR = smoke_logs

        # 2f. a decoy node named Targets must not spawn duplicates
        real_targets = slicer.mrmlScene.GetFirstNodeByName("Targets")
        if real_targets is not None:
            slicer.mrmlScene.RemoveNode(real_targets)
        slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", "Targets")
        qt.QApplication.sendEvent(view_target, make_press(centre_x + 8, centre_y - 8))
        slicer.app.processEvents()
        named = slicer.mrmlScene.GetNodesByName("Targets")
        named_list = [named.GetItemAsObject(i) for i in range(named.GetNumberOfItems())]
        check(
            "decoy Targets node does not spawn duplicates",
            len(named_list) == 1 and named_list[0].IsA("vtkMRMLMarkupsFiducialNode"),
            f"nodes={[n.GetClassName() for n in named_list]}",
        )

        # 2g. the same for a decoy named VirtualTip
        real_tip = slicer.mrmlScene.GetFirstNodeByName("VirtualTip")
        if real_tip is not None:
            slicer.mrmlScene.RemoveNode(real_tip)
        slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "VirtualTip")
        qt.QApplication.sendEvent(view_target, make_press(centre_x - 8, centre_y + 8))
        slicer.app.processEvents()
        named_tip = slicer.mrmlScene.GetNodesByName("VirtualTip")
        tip_list = [
            named_tip.GetItemAsObject(i) for i in range(named_tip.GetNumberOfItems())
        ]
        check(
            "decoy VirtualTip node does not spawn duplicates",
            len(tip_list) == 1 and tip_list[0].IsA("vtkMRMLModelNode"),
            f"nodes={[n.GetClassName() for n in tip_list]}",
        )

        # 2h. rapid clicks: every click lands, labels stay unique
        targets_now = slicer.mrmlScene.GetFirstNodeByName("Targets")
        placed_before = widget.logic.placed
        for dx, dy in ((-15, -5), (-12, 2), (-9, 9)):
            qt.QApplication.sendEvent(
                view_target, make_press(centre_x + dx, centre_y + dy)
            )
        slicer.app.processEvents()
        labels = [
            targets_now.GetNthControlPointLabel(i)
            for i in range(targets_now.GetNumberOfControlPoints())
        ]
        check(
            "rapid clicks keep labels unique",
            widget.logic.placed == placed_before + 3
            and len(labels) == len(set(labels))
            and bool(labels),
            f"placed={widget.logic.placed} labels={labels}",
        )

        # 3. the click modes are exclusive: camera releases pick and probe,
        #    and releasing probe must not stop the stream timer
        widget.pickBox.checked = True
        logic = widget.logic
        logic._stream_timer = qt.QTimer()
        logic._stream_timer.start(1000)
        logic._probe_timer = qt.QTimer()
        logic._probe_timer.start(1000)
        widget.probeBox.checked = True
        widget.cameraBox.checked = True
        check(
            "camera mode releases the legacy click modes",
            not widget.pickBox.checked and not widget.probeBox.checked,
            f"pick={widget.pickBox.checked} probe={widget.probeBox.checked}",
        )
        check(
            "camera mode keeps the stream timer alive",
            logic._stream_timer.isActive() and not logic._probe_timer.isActive(),
            f"stream={logic._stream_timer.isActive()} "
            f"probe={logic._probe_timer.isActive()}",
        )
        widget.cameraBox.checked = False
        logic._stream_timer.stop()
        logic._probe_timer.stop()
        widget.pickBox.checked = True

        # 4. Ctrl+left pans the view without placing a camera click
        red_node = red_widget.sliceLogic().GetSliceNode()
        before_pan = [red_node.GetSliceToRAS().GetElement(axis, 3)
                      for axis in range(3)]
        before_click = widget.lastClick
        was_click = view_filter.click_enabled
        view_filter.click_enabled = False
        ctrl_press = make_mouse(
            _MOUSE_PRESS, centre_x, centre_y,
            _LEFT_BUTTON, _LEFT_BUTTON, ctrl)
        check(
            "Ctrl press passes through to establish the mouse grab",
            view_filter.eventFilter(view_target, ctrl_press) is False,
        )
        qt.QApplication.sendEvent(view_target, ctrl_press)
        qt.QApplication.sendEvent(
            view_target,
            make_mouse(mouse_move, centre_x + 20, centre_y,
                       no_button, _LEFT_BUTTON, ctrl))
        qt.QApplication.sendEvent(
            view_target,
            make_mouse(mouse_release, centre_x + 20, centre_y,
                       _LEFT_BUTTON, no_button, ctrl))
        slicer.app.processEvents()
        after_pan = [red_node.GetSliceToRAS().GetElement(axis, 3)
                     for axis in range(3)]
        check(
            "Ctrl+left pans without placing a camera click",
            after_pan != before_pan and widget.lastClick == before_click,
            f"before={before_pan} after={after_pan} "
            f"click={widget.lastClick!r}",
        )
        matrix = red_node.GetSliceToRAS()
        for axis, value in enumerate(before_pan):
            matrix.SetElement(axis, 3, value)
        red_node.UpdateMatrices()
        red_node.Modified()
        view_filter.click_enabled = was_click

        # 5. Ctrl+left in the 3D view rotates the camera, not the furniture
        three_d = layout.threeDWidget(0)
        three_view = three_d.threeDView() if three_d is not None else None
        three_filter = getattr(widget, "threeDFilter", None)
        if three_view is not None and three_filter is not None:
            three_target = three_filter[0]
            camera = (three_view.renderWindow().GetRenderers()
                      .GetFirstRenderer().GetActiveCamera())
            before_camera = list(camera.GetPosition())
            tx = int(three_target.width // 2)
            ty = int(three_target.height // 2)
            three_press = make_mouse(
                _MOUSE_PRESS, tx, ty, _LEFT_BUTTON, _LEFT_BUTTON, ctrl)
            check(
                "3D Ctrl press passes through to establish the mouse grab",
                three_filter[1].eventFilter(three_target, three_press) is False,
            )
            qt.QApplication.sendEvent(three_target, three_press)
            qt.QApplication.sendEvent(
                three_target,
                make_mouse(mouse_move, tx + 60, ty + 30, no_button,
                           _LEFT_BUTTON, ctrl))
            qt.QApplication.sendEvent(
                three_target,
                make_mouse(mouse_release, tx + 60, ty + 30, _LEFT_BUTTON,
                           no_button, ctrl))
            slicer.app.processEvents()
            after_camera = list(camera.GetPosition())
            check("Ctrl+left drags the 3D view",
                  after_camera != before_camera,
                  f"before={before_camera} after={after_camera}")
        else:
            check("Ctrl+left drags the 3D view", False,
                  "no 3D view or filter")

        # 6. pick mode off: clicks are not consumed any more
        widget.pickBox.checked = False
        before = widget.lastClick
        qt.QApplication.sendEvent(view_target, make_press(centre_x - 20, centre_y))
        slicer.app.processEvents()
        check("pick off: click not captured", widget.lastClick == before)

        # 7. leaving the module removes the filters
        widget.exit()
        check("filters removed on exit",
              widget.filters == {} and widget.threeDFilter is None)

        failed = [name for name, ok, _detail in _checks if not ok]
        if failed:
            print("SMOKE FAIL: " + ", ".join(failed))
        else:
            print(f"SMOKE PASS {len(_checks)}/{len(_checks)}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("SMOKE FAIL: exception")

    try:
        import slicer  # noqa: PLC0415

        slicer.app.quit()
    except Exception:  # noqa: BLE001
        pass


main()
