"""Tests for the field-cube builder's pure parts.

The Slicer-facing part is exercised by running the script inside Slicer; what
is pinned here is the geometry promise: the drawn cube must be exactly the
field frame.json declares, or the visual aid would lie about where the field
is.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import make_field_cube as cube  # noqa: E402


def test_field_geometry_matches_frame_json():
    size, centre = cube.field_geometry()
    assert size == {"R": 100.0, "A": 100.0, "S": 100.0}
    assert centre == {"R": 0.0, "A": 0.0, "S": 0.0}


def test_cube_bounds_are_the_field_corners():
    size, centre = cube.field_geometry()
    lower, upper = cube.cube_bounds(size, centre)
    assert lower == (-50.0, -50.0, -50.0)
    assert upper == (50.0, 50.0, 50.0)


def test_the_cube_is_opaque_wireframe_not_translucent():
    """Transparency forces depth peeling and stalls point drags in the 3D view."""
    assert cube.CUBE_OPACITY == 1.0
    assert cube.CUBE_REPRESENTATION == "wireframe"


def test_cube_bounds_follow_a_moved_field(tmp_path):
    path = tmp_path / "frame.json"
    path.write_text(
        """
{
  "field_size_mm": {"R": 40.0, "A": 60.0, "S": 20.0},
  "field_center_ras": {"R": 10.0, "A": 0.0, "S": -5.0},
  "view_axis_map": {}
}
"""
    )
    size, centre = cube.field_geometry(path)
    lower, upper = cube.cube_bounds(size, centre)
    assert lower == (-10.0, -30.0, -15.0)
    assert upper == (30.0, 30.0, 5.0)


def test_the_script_never_derives_paths_from_dunder_file():
    """Same console rule as check_live_mapping.py: exec() has no real __file__.

    Guarded use inside _resolve_project_root is allowed: it only counts when
    the path is a real file under a checkout marker, otherwise it falls back
    to the explicit override / env / hardcoded root.
    """
    source = pathlib.Path(cube.__file__).read_text()
    assert "_resolve_project_root" in source
    assert "PROJECT_ROOT_OVERRIDE" in source
    assert "HIMX_CAM_ROOT" in source
    lines = source.splitlines()
    in_resolver = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("def _resolve_project_root"):
            in_resolver = True
            continue
        if in_resolver and stripped.startswith("PROJECT_ROOT = "):
            break
        if not in_resolver:
            assert "__file__" not in stripped, f"uses __file__: {stripped!r}"
    assert "PROJECT_ROOT = _resolve_project_root()" in source
