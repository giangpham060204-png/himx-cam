"""Create the 5 reference points and read their positions back.

Run from Slicer's Python Console:

    exec(open('<path-to-himx-cam>/tools/make_refpoints.py').read())

Why this is done numerically rather than by dragging: the reference points
must sit at exact known values for the check to mean anything. Dragging with a
mouse gives a position you then read back off the screen, which measures your
clicking rather than the converter. Setting the numbers directly leaves the
converter as the only thing under test.

The 5 points cover the field's corners and centre, because a sign error shows
up as roughly one field width at a corner but is nearly invisible at the
centre.
"""

import json

NODE_NAME = "RefPoints"

# Cube-frame targets, and the RAS values that should produce them.
# Cube counts from the field corner; RAS counts from the field centre.
# Cube (0,0,0) is RAS (-50,-50,-50) for a 100 mm field.
REFERENCE_POINTS = [
    ("P1_corner", (-50.0, -50.0, -50.0), (0.0, 0.0, 0.0)),
    ("P2_R100", (50.0, -50.0, -50.0), (100.0, 0.0, 0.0)),
    ("P3_A100", (-50.0, 50.0, -50.0), (0.0, 100.0, 0.0)),
    ("P4_S100", (-50.0, -50.0, 50.0), (0.0, 0.0, 100.0)),
    ("P5_centre", (0.0, 0.0, 0.0), (50.0, 50.0, 50.0)),
]


def _node(slicer):
    node = slicer.mrmlScene.GetFirstNodeByName(NODE_NAME)
    if node is None or not node.IsA("vtkMRMLMarkupsFiducialNode"):
        node = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLMarkupsFiducialNode", NODE_NAME
        )
    if not node.GetDisplayNode():
        node.CreateDefaultDisplayNodes()
    return node


def build():
    """Create the reference point list and return the node."""
    import slicer

    node = _node(slicer)
    node.GetDisplayNode().SetPointLabelsVisibility(True)
    node.GetDisplayNode().SetGlyphScale(3.0)
    node.RemoveAllControlPoints()

    for label, ras, _cube in REFERENCE_POINTS:
        index = node.AddControlPoint(ras[0], ras[1], ras[2])
        node.SetNthControlPointLabel(index, label)

    return node


def report(node):
    """Print label, RAS read back from Slicer, and the expected Cube value."""
    lines = ["label        RAS (from Slicer)          expected Cube XYZ"]
    rows = []
    for i in range(node.GetNumberOfControlPoints()):
        label = node.GetNthControlPointLabel(i)
        position = node.GetNthControlPointPosition(i)
        expected = dict((name, cube) for name, _ras, cube in REFERENCE_POINTS)[label]
        lines.append(
            f"{label:12s} [{position[0]:8.3f} {position[1]:8.3f} {position[2]:8.3f}]"
            f"      [{expected[0]:6.1f} {expected[1]:6.1f} {expected[2]:6.1f}]"
        )
        rows.append(
            {
                "label": label,
                "ras": [float(c) for c in position],
                "cube": list(expected),
            }
        )
    return "\n".join(lines), rows


def run():
    import json

    node = build()
    text, rows = report(node)
    print(text)
    print()
    print(json.dumps(rows))
    print()
    print("Paste the JSON above; it is checked against targeting/converter.py.")


if __name__ in ("__main__", "__builtin__", "builtins"):
    run()
else:
    try:
        run()
    except Exception as exc:  # noqa: BLE001 - console bootstrap
        print(f"{type(exc).__name__}: {exc}")