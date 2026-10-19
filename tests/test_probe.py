"""Tests for the probe file a person moves in Slicer.

The reader's whole job is to be unexciting: read what Slicer wrote, or say why
it cannot. It must never invent a point, because the point it would invent is
one the cameras would then be asked to agree with.
"""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.probe import PROBE_FILE, Probe, read_probe  # noqa: E402

CUBE = (50.0, 51.5, 52.25)
RAS = (0.0, 1.5, 2.25)


def write_probe(folder, **fields):
    payload = {"cube": CUBE, "ras": RAS, "view": "XY", "when": "2026-10-19T10:00:00"}
    payload.update(fields)
    path = pathlib.Path(folder) / PROBE_FILE
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_published_probe_reads_back_with_its_numbers(tmp_path):
    path = write_probe(tmp_path)

    probe, note = read_probe(path)

    assert probe == Probe(cube=CUBE, ras=RAS, view="XY",
                          when="2026-10-19T10:00:00")
    assert PROBE_FILE in note


def test_a_missing_probe_is_a_sentence_not_a_crash(tmp_path):
    probe, note = read_probe(tmp_path / PROBE_FILE)

    assert probe is None
    assert PROBE_FILE in note


def test_a_half_written_file_is_refused_not_guessed(tmp_path):
    """The writer renames over the old file, so this should never happen --
    and if it does, the reader must not produce a point from it."""
    path = pathlib.Path(tmp_path) / PROBE_FILE
    path.write_text('{"cube": [50.0, ', encoding="utf-8")

    probe, note = read_probe(path)

    assert probe is None
    assert "not a probe file" in note


def test_a_file_without_a_cube_is_refused_by_name(tmp_path):
    path = pathlib.Path(tmp_path) / PROBE_FILE
    path.write_text(json.dumps({"ras": RAS}), encoding="utf-8")

    probe, note = read_probe(path)

    assert probe is None
    assert "missing cube" in note


def test_text_where_numbers_belong_is_refused(tmp_path):
    path = write_probe(tmp_path, cube=["left", "middle", "right"])

    probe, note = read_probe(path)

    assert probe is None
    assert "three numbers" in note


def test_two_numbers_are_not_a_point(tmp_path):
    path = write_probe(tmp_path, ras=[1.0, 2.0])

    probe, note = read_probe(path)

    assert probe is None
    assert "three numbers" in note


def test_view_and_when_are_optional(tmp_path):
    path = pathlib.Path(tmp_path) / PROBE_FILE
    path.write_text(json.dumps({"cube": CUBE, "ras": RAS}), encoding="utf-8")

    probe, _ = read_probe(path)

    assert probe is not None
    assert probe.view == "" and probe.when == ""
