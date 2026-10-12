"""Build the operating field cube so it shows in every view.

Run in Slicer's Python Console:

    exec(open('/home/gianghp/projects/himx-cam/tools/make_field_cube.py').read())

A fresh Slicer session starts with an empty scene, and the cube is not saved
anywhere, so it must be rebuilt after each Slicer restart. This script also
prints what displayable nodes the scene already had, so "the cube is missing"
and "the cube is merely hidden" are easy to tell apart.

The cube spans exactly the field frame.json describes: 100mm per side,
centred on RAS (0,0,0). It is translucent with visible edges, and it is shown
in the 3D view and in all three slice views, so it can be aimed at during the
hover check. If the field ever changes, edit frame.json, not this file.
"""

from __future__ import annotations

import pathlib

# Hardcoded on purpose: exec() in Slicer's console does not provide a truthful
# __file__, so paths must not be derived from it. See check_live_mapping.py.
PROJECT_ROOT = pathlib.Path("/home/gianghp/projects/himx-cam")
FRAME_PATH = PROJECT_ROOT / "frame.json"

NODE_NAME = "FieldCube"
CUBE_COLOR = (0.90, 0.45, 0.40)
CUBE_OPACITY = 0.35

_RAS_AXES = ("R", "A", "S")


def field_geometry(path=FRAME_PATH):
    """Size (mm per axis) and centre (RAS mm) of the operating field."""
    import json  # noqa: PLC0415

    data = json.loads(pathlib.Path(path).read_text())
    size = {axis: float(data["field_size_mm"][axis]) for axis in _RAS_AXES}
    centre = {axis: float(data["field_center_ras"][axis]) for axis in _RAS_AXES}
    return size, centre


def cube_bounds(size, centre):
    """The lower and upper RAS corners the cube spans."""
    lower = tuple(centre[axis] - size[axis] / 2.0 for axis in _RAS_AXES)
    upper = tuple(centre[axis] + size[axis] / 2.0 for axis in _RAS_AXES)
    return lower, upper


def _scene_inventory(slicer):
    """One line per displayable node, so a hidden cube is easy to spot."""
    lines = []
    for class_name, label in (
        ("vtkMRMLModelNode", "model"),
        ("vtkMRMLMarkupsROINode", "ROI"),
        ("vtkMRMLMarkupsFiducialNode", "points"),
        ("vtkMRMLVolumeNode", "volume"),
    ):
        for node in slicer.util.getNodesByClass(class_name):
            detail = ""
            if label == "model":
                display = node.GetDisplayNode()
                if display is not None:
                    detail = (
                        f"  (3D visible: {bool(display.GetVisibility())},"
                        f" 2D visible: {bool(display.GetVisibility2D())})"
                    )
            lines.append(f"  {label} {node.GetName()!r}{detail}")
    return lines


def run():
    import slicer  # noqa: PLC0415
    import vtk  # noqa: PLC0415

    size, centre = field_geometry()
    lower, upper = cube_bounds(size, centre)
    print(
        f"field: {size['R']:.1f} x {size['A']:.1f} x {size['S']:.1f} mm, "
        f"centred on RAS ({centre['R']:.1f}, {centre['A']:.1f}, {centre['S']:.1f})"
    )
    print(f"cube spans RAS {lower} to {upper}")

    inventory = _scene_inventory(slicer)
    print("scene before this script ran:")
    print("\n".join(inventory) if inventory else "  (no displayable nodes)")

    old = slicer.mrmlScene.GetFirstNodeByName(NODE_NAME)
    if old is not None:
        slicer.mrmlScene.RemoveNode(old)

    source = vtk.vtkCubeSource()
    source.SetXLength(size["R"])
    source.SetYLength(size["A"])
    source.SetZLength(size["S"])
    source.SetCenter(centre["R"], centre["A"], centre["S"])
    source.Update()

    model = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", NODE_NAME)
    model.SetAndObservePolyData(source.GetOutput())
    model.CreateDefaultDisplayNodes()

    display = model.GetDisplayNode()
    display.SetColor(*CUBE_COLOR)
    display.SetOpacity(CUBE_OPACITY)
    display.SetEdgeVisibility(True)
    # Visibility2D is the current API; the older SliceIntersectionVisibility
    # setter is deprecated and prints a warning (checked against Slicer 5.12.3).
    display.SetVisibility2D(True)
    display.SetVisibility(True)

    slicer.util.resetSliceViews()
    slicer.app.processEvents()
    print(
        f"{NODE_NAME} created: translucent, edges on, visible in 3D and all "
        "slice views."
    )
    print("next: run tools/check_live_mapping.py and hold the mouse still on")
    print("three spots inside the cube.")


if __name__ in ("__main__", "__builtin__", "builtins"):
    run()
else:
    try:
        run()
    except Exception as exc:  # noqa: BLE001 - console bootstrap
        print(f"{type(exc).__name__}: {exc}")
