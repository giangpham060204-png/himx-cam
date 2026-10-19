"""The clicks file, and the arithmetic that turns a view position into a pixel."""

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.clicks import CLICKS_FILE, Clicks, pixel_from_view, read_clicks

PIXELS = [[10.0, 20.0], [110.5, 120.25], [300.0, 40.0]]
IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def write_clicks(folder, **fields):
    payload = {"pixels": PIXELS, "when": "2026-10-19T10:00:00"}
    payload.update(fields)
    path = pathlib.Path(folder) / CLICKS_FILE
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_published_click_set_reads_back(tmp_path):
    clicks, note = read_clicks(write_clicks(tmp_path))

    assert clicks == Clicks(pixels=tuple(tuple(pair) for pair in PIXELS),
                            when="2026-10-19T10:00:00")
    assert CLICKS_FILE in note


def test_a_missing_clicks_file_is_a_sentence(tmp_path):
    clicks, note = read_clicks(tmp_path / CLICKS_FILE)

    assert clicks is None and CLICKS_FILE in note


def test_a_half_written_file_is_refused(tmp_path):
    path = pathlib.Path(tmp_path) / CLICKS_FILE
    path.write_text('{"pixels": [[1.0, ', encoding="utf-8")

    clicks, note = read_clicks(path)

    assert clicks is None and "not a clicks file" in note


def test_two_pixels_are_not_a_click_set(tmp_path):
    clicks, note = read_clicks(write_clicks(tmp_path, pixels=PIXELS[:2]))

    assert clicks is None and "three" in note


def test_text_where_numbers_belong_is_refused(tmp_path):
    clicks, note = read_clicks(write_clicks(tmp_path, pixels=[["a", "b"]] * 3))

    assert clicks is None and "numbers" in note


def test_the_mapping_is_the_identity_for_identity_matrices():
    assert pixel_from_view(IDENTITY, IDENTITY, 37.0, 91.0) == pytest.approx((37.0, 91.0))


def test_a_flipped_view_flips_the_pixel():
    """Slicer's y grows down; a picture whose y grows up would flip here."""
    flipped = [[1.0, 0.0, 0.0, 0.0], [0.0, -1.0, 0.0, 320.0],
               [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]

    u, v = pixel_from_view(IDENTITY, flipped, 10.0, 20.0)

    assert u == pytest.approx(10.0)
    assert v == pytest.approx(300.0), "a flip must move the pixel, not the same one"


def test_the_view_scale_is_honoured():
    scaled = [[2.0, 0.0, 0.0, 5.0], [0.0, 2.0, 0.0, 7.0],
              [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]

    assert pixel_from_view(scaled, IDENTITY, 10.0, 10.0) == pytest.approx((25.0, 27.0))


def test_a_singular_view_is_refused_not_guessed():
    singular = [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]

    with pytest.raises(ValueError, match="no mapping"):
        pixel_from_view(singular, IDENTITY, 1.0, 2.0)
