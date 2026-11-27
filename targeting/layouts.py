"""The view layout the Targeting module asks Slicer to show.

Pure data: no Slicer import, so the tests can read it and the panel cannot
describe one arrangement while the module builds another.

Why a custom layout at all: a camera picture is evidence, not a coordinate
space. Borrowing Red/Green/Yellow for the three feeds means every view that
could show the plate is showing a photograph instead, and the click mapping
has to remember which colour is which camera. With c1/c2/c3 in slice views of
their own, the standard views keep their normal meaning and the camera panes
are named after the camera they carry.

Slicer reserves layout ids below 100 for its own arrangements, so this one
uses 812. ``<layout split="true">`` leaves the dividers draggable.
"""

CAMERA_LAYOUT_ID = 812
CAMERA_VIEWS = ("Cam1", "Cam2", "Cam3")
CAMERA_VIEW_LABELS = ("c1", "c2", "c3")
# The same red/green/orange the camera click marks already use.
CAMERA_VIEW_COLOURS = ("#F34A33", "#1CA01C", "#FF7F0E")


def camera_layout_xml():
    """Three camera panes across the top, one wide 3D view below.

    The camera panes are 4:3, the shape of a 160x120 tile, so the picture
    fills the pane instead of sitting in black pillars. The 3D view spans the
    full width: it is where the plate and the measured point appear, and it
    is the half that benefits from width.
    """
    cells = "\n".join(
        "      <item>"
        f'<view class="vtkMRMLSliceNode" singletontag="{name}">'
        '<property name="orientation" action="default">Axial</property>'
        f'<property name="viewlabel" action="default">{label}</property>'
        f'<property name="viewcolor" action="default">{colour}</property>'
        '</view></item>'
        for name, label, colour in zip(
            CAMERA_VIEWS, CAMERA_VIEW_LABELS, CAMERA_VIEW_COLOURS))
    return (
        '<layout type="vertical" split="true">\n'
        '  <item><layout type="horizontal" split="true">\n'
        f'{cells}\n'
        '  </layout></item>\n'
        '  <item><view class="vtkMRMLViewNode" singletontag="1">\n'
        '    <property name="viewlabel" action="default">3D</property>\n'
        '  </view></item>\n'
        '</layout>\n'
    )
