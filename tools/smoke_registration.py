"""In-Slicer smoke test for the virtual -> real registration panel.

Runs in a separate Slicer window:

    timeout 300 <slicer> --no-splash --python-script tools/smoke_registration.py

Builds the Targeting widget, points its registration file at a temp JSON,
fits a known rigid transform, maps a virtual target, and checks the refusals.
Prints one line per check and finally "SMOKE PASS n/n" or "SMOKE FAIL: names".
"""

import json
import math
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
                parent / "targeting" / "registration.py").is_file():
            return parent
    raise RuntimeError("could not locate the himx-cam checkout")


PROJECT_ROOT = _resolve_project_root()
sys.path.insert(0, str(PROJECT_ROOT / "SlicerTargeting" / "Targeting"))
sys.path.insert(0, str(PROJECT_ROOT))

_checks = []


def check(name, ok, detail=""):
    _checks.append((name, bool(ok), detail))
    print(f"CHECK {name}: {'ok' if ok else 'FAIL'} {detail}")


def _rotation_z(degrees):
    import numpy as np

    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def main():
    try:
        import qt

        import Targeting as targeting_module
        from targeting import registration

        temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="registration-smoke-"))
        registration.DEFAULT_PATH = temp_dir / "marker-registration.json"

        import numpy as np
        rotation = _rotation_z(12.0)
        translation = np.array([100.0, 50.0, 10.0])
        virtual = [(0.0, 0.0, 0.0), (60.0, 0.0, 0.0), (30.0, 20.0, 0.0)]
        markers = []
        for index, point in enumerate(virtual, start=1):
            real = rotation @ np.array(point) + translation
            markers.append({
                "label": f"M{index}",
                "virtual_mm": list(point),
                "real_mm": [float(value) for value in real],
            })
        registration.DEFAULT_PATH.write_text(json.dumps({
            "real_frame": "arm-base",
            "markers": markers,
        }), encoding="utf-8")

        panel = qt.QWidget()
        panel.setLayout(qt.QVBoxLayout())
        widget = targeting_module.TargetingWidget(panel)
        widget.setup()
        widget.enter()

        check(
            "the registration panel is in the main panel, not Advanced",
            hasattr(widget, "registrationTable")
            and widget.registrationTable.columnCount == 7
            and not widget.advancedPanel.isAncestorOf(widget.registrationTable),
            f"columns={getattr(widget.registrationTable, 'columnCount', None)}",
        )
        check(
            "the registration file loads three rows",
            widget.registrationTable.rowCount == 3,
            f"rows={widget.registrationTable.rowCount}",
        )
        check(
            "the real frame is shown",
            widget.realFrameEdit.text == "arm-base",
            widget.realFrameEdit.text,
        )

        widget.onRegistrationFit()
        fit = widget.registrationFit
        check(
            "the known rigid transform fits with zero residual",
            fit is not None
            and abs(fit["rms"]) < 0.01
            and abs(fit["scale"] - 1.0) < 1e-4,
            "" if fit is None else f"rms={fit['rms']:.3g} scale={fit['scale']:.6f}",
        )
        solved = PROJECT_ROOT / "logs" / "registration-solved.json"
        check(
            "the solved map is written beside the logs",
            solved.is_file(),
            str(solved),
        )

        widget.lastCube = (30.0, 10.0, 0.0)
        expected = rotation @ np.array(widget.lastCube) + translation
        widget.onRegistrationMap()
        check(
            "the last target maps to the real frame",
            widget.lastReal is not None
            and np.allclose(np.asarray(widget.lastReal), expected, atol=1e-6),
            f"real={widget.lastReal}",
        )
        check(
            "the mapped point is shown with its frame",
            "real arm-base" in widget.registrationStatusLabel.text,
            widget.registrationStatusLabel.text.replace("\n", " | "),
        )
        last = PROJECT_ROOT / "logs" / "registration-last.json"
        check(
            "the last map is written beside the logs",
            last.is_file(),
            str(last),
        )

        widget.registrationFit = None
        widget.onRegistrationMap()
        check(
            "mapping refuses before a fit",
            "fit the markers first" in widget.registrationStatusLabel.text,
            widget.registrationStatusLabel.text,
        )

        # Collinear real markers must be refused, not fitted.
        widget.registrationTable.setRowCount(0)
        for index, (virtual_point, real_point) in enumerate((
                ((0, 0, 0), (0, 0, 0)),
                ((60, 0, 0), (30, 0, 0)),
                ((0, 40, 0), (60, 0, 0))), start=1):
            row = widget.registrationTable.rowCount
            widget.registrationTable.insertRow(row)
            widget.registrationTable.setItem(
                row, 0, qt.QTableWidgetItem(f"C{index}"))
            for column, value in enumerate((*virtual_point, *real_point),
                                           start=1):
                widget.registrationTable.setItem(
                    row, column, qt.QTableWidgetItem(str(value)))
        widget.onRegistrationFit()
        check(
            "collinear real markers are refused",
            widget.registrationFit is None
            and "collinear" in widget.registrationStatusLabel.text,
            widget.registrationStatusLabel.text,
        )

        # Save and load round-trip through the patched path.
        widget._fill_registration_table(registration.load_registration(
            registration.DEFAULT_PATH))
        widget.onRegistrationSave()
        saved = json.loads(registration.DEFAULT_PATH.read_text(encoding="utf-8"))
        check(
            "save writes the measured registration",
            saved["real_frame"] == "arm-base" and len(saved["markers"]) == 3,
            str(registration.DEFAULT_PATH),
        )
        saved["real_frame"] = "bench"
        registration.DEFAULT_PATH.write_text(json.dumps(saved), encoding="utf-8")
        widget.onRegistrationLoad()
        check(
            "load reads the real frame and rows",
            widget.realFrameEdit.text == "bench"
            and widget.registrationTable.rowCount == 3,
            f"frame={widget.realFrameEdit.text} rows={widget.registrationTable.rowCount}",
        )

        check(
            "the camera layout and canvas are untouched",
            widget.cameraLayoutButton is not None
            and hasattr(widget, "realCameraButton"),
            "",
        )

        failed = [name for name, ok, _detail in _checks if not ok]
        print("SMOKE PASS %d/%d" % (len(_checks) - len(failed), len(_checks))
              if not failed else "SMOKE FAIL: " + ", ".join(failed))
        return 0 if not failed else 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
