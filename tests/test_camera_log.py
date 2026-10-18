"""Tests for the camera system's log.

Columns are matched by name so a future extra column cannot shift the meaning of
the ones after it, and every row that cannot be read is named rather than
dropped in silence. A log reader that quietly reads the wrong column and reports
a plausible number is the worst thing this file could be.
"""

import pathlib
import sys
from datetime import datetime

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.camera_log import (  # noqa: E402
    HEADER,
    append_camera_target,
    camera_log_path,
    format_camera_row,
    read_camera_targets,
)

CUBE = (1.0, 2.0, 3.0)
RAS = (-48.0, -47.0, -46.0)
PIXELS = ((160.0, 160.0), (158.5, 161.25), (161.0, 159.0))
WHEN = datetime(2026, 10, 19, 14, 30, 0)


def test_the_header_is_the_declared_one():
    # a guard against silent schema drift: changing a column name or its
    # position is a deliberate act, not something that happens by editing
    assert HEADER == (
        "timestamp", "source", "cube_x", "cube_y", "cube_z",
        "ras_r", "ras_a", "ras_s", "residual_px",
        "u1_px", "v1_px", "u2_px", "v2_px", "u3_px", "v3_px",
    )


def test_the_filename_carries_the_day():
    assert camera_log_path("logs", WHEN).name == "camera-targets-2026-10-19.csv"


def test_a_new_file_gets_a_header_then_the_row(tmp_path):
    path = append_camera_target(tmp_path, cube=CUBE, ras=RAS, residual_px=0.21,
                                pixels=PIXELS, when=WHEN)

    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(HEADER)
    assert len(lines) == 2


def test_a_second_append_does_not_repeat_the_header(tmp_path):
    append_camera_target(tmp_path, cube=CUBE, ras=RAS, residual_px=0.21,
                         pixels=PIXELS, when=WHEN)
    path = append_camera_target(tmp_path, cube=CUBE, ras=RAS, residual_px=0.22,
                                pixels=PIXELS, when=WHEN)

    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert lines[0] == ",".join(HEADER)


def test_a_written_row_reads_back_the_same(tmp_path):
    append_camera_target(tmp_path, cube=CUBE, ras=RAS, residual_px=0.21,
                         pixels=PIXELS, when=WHEN)

    log = read_camera_targets(camera_log_path(tmp_path, WHEN))

    assert log.problems == ()
    assert len(log.targets) == 1
    target = log.targets[0]
    assert target.cube == pytest.approx(CUBE)
    assert target.ras == pytest.approx(RAS)
    assert target.residual_px == pytest.approx(0.21)
    flat = [value for pair in target.pixels for value in pair]
    assert flat == pytest.approx([value for pair in PIXELS for value in pair])
    assert target.source == "simulator"
    assert target.timestamp == WHEN.isoformat(timespec="seconds")


def test_an_extra_column_is_ignored_rather_than_shifting_the_rest(tmp_path):
    path = tmp_path / "camera-targets-2026-10-19.csv"
    path.write_text(",".join(HEADER + ("note",)) + "\n"
                    + format_camera_row(cube=CUBE, ras=RAS, residual_px=0.21,
                                        pixels=PIXELS, when=WHEN) + ",hello\n")

    log = read_camera_targets(path)

    assert log.problems == ()
    assert log.targets[0].cube == pytest.approx(CUBE)


def test_a_file_missing_a_required_column_is_rejected_whole(tmp_path):
    path = tmp_path / "camera-targets-2026-10-19.csv"
    path.write_text(",".join(name for name in HEADER if name != "ras_s") + "\n")

    log = read_camera_targets(path)

    assert log.targets == ()
    assert "ras_s" in log.problems[0]
    assert path.name in log.problems[0]


def test_a_short_row_is_named_with_its_line(tmp_path):
    path = tmp_path / "camera-targets-2026-10-19.csv"
    path.write_text(",".join(HEADER) + "\n0,1,2\n")

    log = read_camera_targets(path)

    assert log.targets == ()
    assert "line 2" in log.problems[0]
    assert path.name in log.problems[0]


def test_a_non_numeric_field_is_named_with_its_line(tmp_path):
    path = tmp_path / "camera-targets-2026-10-19.csv"
    good = format_camera_row(cube=CUBE, ras=RAS, residual_px=0.21, pixels=PIXELS, when=WHEN)
    broken = good.replace(",0.2100,", ",not-a-number,")
    path.write_text(",".join(HEADER) + "\n" + broken + "\n")

    log = read_camera_targets(path)

    assert log.targets == ()
    assert "line 2" in log.problems[0]


def test_one_bad_row_does_not_lose_the_good_ones(tmp_path):
    path = tmp_path / "camera-targets-2026-10-19.csv"
    good = format_camera_row(cube=CUBE, ras=RAS, residual_px=0.21, pixels=PIXELS, when=WHEN)
    path.write_text(",".join(HEADER) + "\n" + good + "\nbroken\n" + good + "\n")

    log = read_camera_targets(path)

    assert len(log.targets) == 2
    assert len(log.problems) == 1


def test_an_empty_existing_file_still_gets_a_header(tmp_path):
    """A touched-but-empty file holds no header; the first row must not land bare."""
    path = camera_log_path(tmp_path, WHEN)
    path.touch()

    out = append_camera_target(tmp_path, cube=CUBE, ras=RAS, residual_px=0.21,
                               pixels=PIXELS, when=WHEN)

    assert out == path
    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(HEADER)
    assert len(lines) == 2
    log = read_camera_targets(path)
    assert log.problems == ()
    assert len(log.targets) == 1
    assert log.targets[0].cube == pytest.approx(CUBE)


def test_reading_a_missing_file_is_reported_not_raised(tmp_path):
    log = read_camera_targets(tmp_path / "nothing-here.csv")

    assert log.targets == ()
    assert "nothing-here.csv" in log.problems[0]
