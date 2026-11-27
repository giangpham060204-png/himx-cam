"""The view layout the Targeting module asks Slicer to show.

Pure data: no Slicer import, so the tests can read it and the panel cannot
describe one arrangement while the module builds another.

Why a custom layout at all: a camera picture is evidence, not a coordinate
space. Borrowing Red/Green/Yellow for the three feeds means every view that
could show the plate is showing a photograph instead, and the click mapping
has to remember which colour is which camera. With c1/c2/c3 in slice views of
their own, the standard views keep their normal meaning and the camera panes
are named after the camera they carry.

The standard views are not removed from this layout: placing a Slicer point in
XY/XZ/YZ is still part of the workflow, so the four-up sits beside the camera
column instead of being replaced by it.

Slicer reserves layout ids below 100 for its own arrangements, so this one
uses 812. ``<layout split="true">`` leaves the dividers draggable.
"""

CAMERA_LAYOUT_ID = 812
CAMERA_VIEWS = ("Cam1", "Cam2", "Cam3")
CAMERA_VIEW_LABELS = ("c1", "c2", "c3")
# The same red/green/orange the camera click marks already use.
CAMERA_VIEW_COLOURS = ("#F34A33", "#1CA01C", "#FF7F0E")

# The built-in four-up: three orthogonal slices and the 3D view.
STANDARD_SLICE_VIEWS = (("Red", "R", "Axial"),
                        ("Green", "G", "Coronal"),
                        ("Yellow", "Y", "Sagittal"))
THREE_D_VIEW = "1"


def _camera_cell(name, label, colour):
    return ('<view class="vtkMRMLSliceNode" singletontag="%s">'
            '<property name="orientation" action="default">Axial</property>'
            '<property name="viewlabel" action="default">%s</property>'
            '<property name="viewcolor" action="default">%s</property>'
            '</view>' % (name, label, colour))


def _slice_cell(name, label, orientation):
    return ('<view class="vtkMRMLSliceNode" singletontag="%s">'
            '<property name="orientation" action="default">%s</property>'
            '<property name="viewlabel" action="default">%s</property>'
            '</view>' % (name, orientation, label))


def camera_layout_xml():
    """Three camera panes in a left column, the standard four-up beside them.

    The camera column is 4:3 (the shape of a 160x120 tile), so each picture
    fills its pane instead of sitting in black bars. The right side is the
    arrangement Slicer users already know: Red/Green on top, Yellow beside the
    3D view below.
    """
    cameras = "\n".join(
        "      <item>%s</item>" % _camera_cell(name, label, colour)
        for name, label, colour in zip(
            CAMERA_VIEWS, CAMERA_VIEW_LABELS, CAMERA_VIEW_COLOURS))
    (red, green) = STANDARD_SLICE_VIEWS[0], STANDARD_SLICE_VIEWS[1]
    (yellow,) = STANDARD_SLICE_VIEWS[2:3]
    return (
        '<layout type="horizontal" split="true">\n'
        '  <item><layout type="vertical" split="true">\n'
        f'{cameras}\n'
        '  </layout></item>\n'
        '  <item><layout type="vertical" split="true">\n'
        '    <item><layout type="horizontal" split="true">\n'
        f'      <item>{_slice_cell(*red)}</item>\n'
        f'      <item>{_slice_cell(*green)}</item>\n'
        '    </layout></item>\n'
        '    <item><layout type="horizontal" split="true">\n'
        f'      <item>{_slice_cell(*yellow)}</item>\n'
        '      <item><view class="vtkMRMLViewNode" singletontag="'
        f'{THREE_D_VIEW}">'
        '<property name="viewlabel" action="default">3D</property>'
        '</view></item>\n'
        '    </layout></item>\n'
        '  </layout></item>\n'
        '</layout>\n'
    )
