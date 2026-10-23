"""Build the operating field cube so it shows in every view.

Run in Slicer's Python Console:

    exec(open('<path-to-himx-cam>/tools/make_field_cube.py').read())

A fresh Slicer session starts with an empty scene, and the cube is not saved
anywhere, so it must be rebuilt after each Slicer restart. This script also
prints what displayable nodes the scene already had, so "the cube is missing"
and "the cube is merely hidden" are easy to tell apart.

The cube spans exactly the field frame.json describes: 100mm per side,
centred on RAS (0,0,0). It is drawn as an opaque wireframe with visible
edges, and it is shown in the 3D view and in all three slice views, so it can
be aimed at during the hover check. The wireframe is deliberate: a
translucent surface makes Slicer's 3D view re-render through its expensive
transparency path, which turns point drags into visible stalls. If the field
ever changes, edit frame.json, not this file.
"""

from __future__ import annotations

import pathlib

# No hardcoded fallback: exec() in Slicer's console does not provide a
# truthful __file__, so guessing silently lands somewhere else. Precedence:
# PROJECT_ROOT_OVERRIDE global > HIMX_CAM_ROOT env > truthful __file__
# marker walk > panic. See check_live_mapping.py.


def _resolve_project_root(_globals=None, _env=None):
    """Find the checkout root without scanning the disk.

    No cwd or HOME crawl: only explicit override, env, or a __file__
    that resolves to a real file under a marker (frame.json +
    targeting/converter.py). Anything else panics instead of guessing.
    """
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
FRAME_PATH = PROJECT_ROOT / "frame.json"

NODE_NAME = "FieldCube"
CUBE_COLOR = (0.90, 0.45, 0.40)
# Opaque wireframe: no transparent surface means no depth-peeling render
# path, so dragging points in the 3D view stays responsive.
CUBE_OPACITY = 1.0
CUBE_REPRESENTATION = "wireframe"

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
    display.SetEdgeColor(*CUBE_COLOR)
    display.SetSelectedColor(*CUBE_COLOR)
    display.SetOpacity(CUBE_OPACITY)
    display.SetEdgeVisibility(True)
    if CUBE_REPRESENTATION == "wireframe":
        display.SetRepresentation(display.WireframeRepresentation)
    # Visibility2D is the current API; the older SliceIntersectionVisibility
    # setter is deprecated and prints a warning (checked against Slicer 5.12.3).
    display.SetVisibility2D(True)
    display.SetVisibility(True)

    slicer.util.resetSliceViews()
    slicer.app.processEvents()
    print(
        f"{NODE_NAME} created: opaque wireframe, edges on, visible in 3D and "
        "all slice views."
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
