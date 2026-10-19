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
from targeting.logbook import (  # noqa: E402
    FILE_PREFIX,
    HEADER,
    append_target,
    format_row,
    log_path,
    newest_target,
    read_targets,
)

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


def test_the_newest_click_target_is_read_back(tmp_path):
    append_target(tmp_path, view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                  cube=(50.0, 50.0, 50.0), ras=(0.0, 0.0, 0.0), when=WHEN)
    append_target(tmp_path, view="YZ", u_px=3.0, v_px=4.0, depth_mm=0.0,
                  cube=(51.0, 52.0, 53.0), ras=(1.0, 2.0, 3.0), when=WHEN)

    target, note = newest_target(tmp_path)

    assert target.cube == pytest.approx((51.0, 52.0, 53.0))
    assert target.view == "YZ"
    assert "targets-2026-10-09.csv" in note


def test_no_click_log_reads_as_a_sentence_not_a_crash(tmp_path):
    target, note = newest_target(tmp_path)

    assert target is None
    assert "no" in note and FILE_PREFIX in note


def test_a_row_that_cannot_be_read_is_named_rather_than_guessed(tmp_path):
    path = log_path(tmp_path, WHEN)
    path.write_text(",".join(HEADER) + "\nbroken\n")

    target, note = newest_target(tmp_path)

    assert target is None
    assert "targets-2026-10-09.csv" in note


def test_the_newest_file_wins(tmp_path):
    append_target(tmp_path, view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                  cube=(1.0, 1.0, 1.0), ras=(0.0, 0.0, 0.0),
                  when=datetime(2026, 10, 1, 9, 0, 0))
    append_target(tmp_path, view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                  cube=(2.0, 2.0, 2.0), ras=(0.0, 0.0, 0.0), when=WHEN)

    target, _ = newest_target(tmp_path)

    assert target.cube == pytest.approx((2.0, 2.0, 2.0))


def test_read_targets_names_every_row_it_cannot_read(tmp_path):
    path = log_path(tmp_path, WHEN)
    good = format_row(view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                      cube=(1.0, 2.0, 3.0), ras=(0.0, 0.0, 0.0), when=WHEN)
    path.write_text(",".join(HEADER) + "\n" + good + "\nbroken\n")

    log = read_targets(path)

    assert len(log.targets) == 1
    assert len(log.problems) == 1 and "line 3" in log.problems[0]
    assert log.targets[0].cube == pytest.approx((1.0, 2.0, 3.0))


def test_reading_a_missing_click_log_is_reported_not_raised(tmp_path):
    log = read_targets(tmp_path / "nothing-here.csv")

    assert log.targets == ()
    assert "nothing-here.csv" in log.problems[0]


def test_an_empty_click_log_gets_a_header_rather_than_a_bare_row(tmp_path):
    """The same defect the camera log had: a touched-but-empty file holds no header."""
    path = log_path(tmp_path, WHEN)
    path.touch()

    append_target(tmp_path, view="XY", u_px=1.0, v_px=2.0, depth_mm=0.0,
                  cube=(1.0, 2.0, 3.0), ras=(0.0, 0.0, 0.0), when=WHEN)

    assert path.read_text().splitlines()[0] == ",".join(HEADER)
    assert read_targets(path).problems == ()
