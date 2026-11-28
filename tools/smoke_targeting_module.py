"""In-Slicer smoke test for the simplified Targeting panel.

Runs in a separate Slicer window:

    DISPLAY=:100 timeout 300 <slicer> \
        --no-splash --python-script tools/smoke_targeting_module.py

Checks the kept UI only: Pick targets, Clear targets, camera controls and
settings, fiducial plate config, virtual -> real config, and placing a target.
Prints one line per check and finally "SMOKE PASS n/n" or "SMOKE FAIL: names".
"""

import pathlib
import sys
import tempfile
import traceback


def _resolve_project_root():
    import os

    env = os.environ.get("HIMX_CAM_ROOT")
    if env:
        return pathlib.Path(env)
    here = pathlib.Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "frame.json").is_file() and (
                parent / "targeting" / "converter.py").is_file():
            return parent
    raise RuntimeError("could not locate the himx-cam checkout")


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

        def make_press(x, y):
            point = qt.QPointF(float(x), float(y))
            return qt.QMouseEvent(
                _MOUSE_PRESS, point, _LEFT_BUTTON, _LEFT_BUTTON, no_modifier)

        # Keep the click log inside the temp area.
        from TargetingLib import logic as logic_module

        smoke_logs = pathlib.Path(tempfile.mkdtemp(prefix="targeting-smoke-"))
        logic_module.LOGS_DIR = smoke_logs

        panel = qt.QWidget()
        panel.setLayout(qt.QVBoxLayout())
        widget = targeting_module.TargetingWidget(panel)
        widget.setup()
        widget.enter()

        check("the legacy Advanced panel is gone",
              not hasattr(widget, "advancedPanel"))
        check("Pick targets is in the main panel",
              hasattr(widget, "pickBox")
              and widget.pickBox.text.startswith("Pick targets"))
        check("Clear targets is beside Pick targets",
              hasattr(widget, "clearButton"))

        check(
            "the camera controls are present",
            all(hasattr(widget, name) for name in
                ("realCameraButton", "realCameraStopButton",
                 "cameraLayoutButton", "videoLabel")),
        )
        check(
            "video settings are configurable",
            all(hasattr(widget, name) for name in
                ("videoSettings", "realCameraDeviceEdit", "fpsSpin",
                 "portSpin", "videoNote")),
        )
        check(
            "the old stream connect/pause controls are gone",
            not any(hasattr(widget, name) for name in
                    ("connectStreamButton", "disconnectStreamButton",
                     "pauseStreamButton", "stepStreamButton",
                     "resumeStreamButton", "streamLabel")),
        )

        check(
            "the fiducial plate is configurable",
            all(hasattr(widget, name) for name in
                ("markerPanel", "markerTable", "markerSaveButton",
                 "markerApplyButton", "markerAddButton", "markerRemoveButton"))
            and not hasattr(widget, "markerLoadButton"),
        )
        check(
            "the virtual -> real table is configurable",
            widget.registrationTable.columnCount == 7
            and all(hasattr(widget, name) for name in
                    ("registrationFitButton", "registrationMapButton",
                     "registrationSaveButton", "registrationAddButton",
                     "registrationRemoveButton"))
            and not any(hasattr(widget, name) for name in
                        ("registrationLoadButton", "registrationSyncButton")),
        )
        check("the real-target readout is at the top",
              hasattr(widget, "realTargetLabel")
              and widget.realTargetLabel.text.startswith("real target:"))

        # Place one target with a real click through the Red-view filter.
        red_widget = slicer.app.layoutManager().sliceWidget("Red")
        red_view = red_widget.sliceView()
        target_widget = widget._mouse_target(red_view)
        centre_x = target_widget.width / 2.0
        centre_y = target_widget.height / 2.0
        widget.pickBox.checked = True
        qt.QApplication.sendEvent(target_widget,
                                  make_press(centre_x, centre_y))
        slicer.app.processEvents()

        targets = slicer.mrmlScene.GetFirstNodeByName("Targets")
        check("a slice click places one target",
              widget.logic.placed == 1
              and targets is not None
              and targets.GetNumberOfControlPoints() == 1,
              f"placed={widget.logic.placed}")
        check("the target is a normal blue point",
              targets is not None
              and targets.GetDisplayNode().GetPointLabelsVisibility() is False
              and tuple(round(value, 3)
                        for value in targets.GetDisplayNode().GetColor())
              == (0.2, 0.4, 1.0)
              and not targets.GetLocked(),
              "labels/colour/lock")
        check("the real-target readout asks for Map",
              "press Map" in widget.realTargetLabel.text,
              widget.realTargetLabel.text)

        widget.onClear()
        check("Clear targets removes the point",
              targets.GetNumberOfControlPoints() == 0
              and widget.lastCube is None,
              f"points={targets.GetNumberOfControlPoints()}")

        widget.exit()
        check("closing removes the click filters",
              not widget.filters and widget.threeDFilter is None)

        failed = [name for name, ok, _detail in _checks if not ok]
        print("SMOKE PASS %d/%d" % (len(_checks) - len(failed), len(_checks))
              if not failed else "SMOKE FAIL: " + ", ".join(failed))
        return 0 if not failed else 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
