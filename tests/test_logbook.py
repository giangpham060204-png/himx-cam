"""Tests for the CSV research log.

Pure stdlib, so these run on the laptop with no Slicer. The log must be
append-only, one file per day, with a header only once, and it must fail
loudly (OSError) when the location cannot be written: a target that cannot
be recorded is a problem the operator has to know about.
"""

import pathlib
import sys
from datetime import datetime

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting import logbook  # noqa: E402

WHEN = datetime(2026, 10, 9, 14, 5, 6)

ROW_KWARGS = dict(
    view="XY",
    u_px=352.0,
    v_px=91.0,
    depth_mm=12.25,
    cube=(48.6, 39.2, 62.3),
    ras=(-1.4, -10.8, 12.25),
)


def test_one_file_per_day(tmp_path):
    assert logbook.log_path(tmp_path, WHEN).name == "targets-2026-10-09.csv"


def test_first_append_writes_header_then_row(tmp_path):
    path = logbook.append_target(tmp_path, when=WHEN, **ROW_KWARGS)
    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(logbook.HEADER)
    assert lines[1] == (
        "2026-10-09T14:05:06,XY,352.0,91.0,12.250,"
        "48.600,39.200,62.300,-1.400,-10.800,12.250"
    )


def test_second_append_has_no_second_header(tmp_path):
    logbook.append_target(tmp_path, when=WHEN, **ROW_KWARGS)
    logbook.append_target(tmp_path, when=WHEN, **ROW_KWARGS)
    lines = logbook.append_target(tmp_path, when=WHEN, **ROW_KWARGS).read_text().splitlines()
    assert len(lines) == 4
    assert lines.count(",".join(logbook.HEADER)) == 1


def test_directories_are_created(tmp_path):
    nested = tmp_path / "a" / "b"
    path = logbook.append_target(nested, when=WHEN, **ROW_KWARGS)
    assert path.exists()


def test_an_unwritable_location_raises_oserror(tmp_path):
    blocked = tmp_path / "blocked.csv"  # a file where a directory is needed
    blocked.write_text("x")
    with pytest.raises(OSError):
        logbook.append_target(blocked, when=WHEN, **ROW_KWARGS)


def test_format_row_accepts_sequences_for_cube_and_ras():
    row = logbook.format_row(
        view="YZ",
        u_px=1.5,
        v_px=2.5,
        depth_mm=0.0,
        cube=[0, 0, 0],
        ras=[-50, -50, -50],
        when=WHEN,
    )
    assert row.endswith("0.000,0.000,0.000,-50.000,-50.000,-50.000")
