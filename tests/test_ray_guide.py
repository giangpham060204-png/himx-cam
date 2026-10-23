"""Geometry of the one-ray guide: a 3D ray and its epipolar line on a picture.

The guide is deliberately not a measurement. It answers one question: given a
click in camera A, where could the matching point appear in camera B? The answer
is a line, and these tests pin down that line and its clipping to the image.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from targeting.ray_guide import clip_segment, epipolar_segment, ray_segment  # noqa: E402
from targeting.triangulation import project, ray_for  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic-camera-rig.json"


def fixture_projections():
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [camera["projection"] for camera in document["cameras"]]


def distance_to_line(point, first, second):
    """Perpendicular distance from a 2D point to the infinite line."""
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    denominator = math.hypot(dx, dy)
    assert denominator > 1e-12
    return abs(dx * (first[1] - point[1]) - dy * (first[0] - point[0])) / denominator


def point_on_segment(point, first, second, epsilon=1e-6):
    """Whether every coordinate of point lies between the segment's ends."""
    for axis in range(2):
        low = min(first[axis], second[axis]) - epsilon
        high = max(first[axis], second[axis]) + epsilon
        if not low <= point[axis] <= high:
            return False
    return distance_to_line(point, first, second) < epsilon


def on_image_edge(point, width, height, epsilon=1e-6):
    return (abs(point[0]) <= epsilon
            or abs(point[0] - (width - 1.0)) <= epsilon
            or abs(point[1]) <= epsilon
            or abs(point[1] - (height - 1.0)) <= epsilon)


def test_an_epipolar_line_spans_the_picture_and_contains_the_matching_click():
    projections = fixture_projections()
    point = (0.0, 0.0, 0.0)
    clicks = [project(projection, point) for projection in projections]
    width = height = 320

    for target in (1, 2):
        segment = epipolar_segment(
            projections[0], clicks[0], projections[target], width, height)
        assert segment is not None
        first, second = segment
        assert point_on_segment(clicks[target], first, second), (
            f"line for c{1}->c{target + 1} stops before its matching click: "
            f"{clicks[target]} not in {segment}")
        for endpoint in (first, second):
            assert on_image_edge(endpoint, width, height), (
                f"line does not span the picture: {segment}")


def test_a_ray_segment_starts_at_the_camera_and_has_the_requested_length():
    projection = fixture_projections()[0]
    pixel = (160.0, 160.0)

    origin, endpoint = ray_segment(projection, pixel, length_mm=1000.0)

    direction = ray_for(projection, pixel)[1]
    assert math.dist(origin, endpoint) == pytest.approx(1000.0)
    for axis in range(3):
        assert endpoint[axis] - origin[axis] == pytest.approx(1000.0 * direction[axis])


def test_an_epipolar_line_is_the_projection_of_the_click_ray():
    projections = fixture_projections()
    clicked = (160.0, 160.0)
    width = height = 320

    segment = epipolar_segment(projections[0], clicked, projections[1], width, height)

    assert segment is not None
    first, second = segment
    for endpoint in (first, second):
        assert 0.0 <= endpoint[0] <= width - 1
        assert 0.0 <= endpoint[1] <= height - 1

    origin, direction = ray_for(projections[0], clicked)
    for length in (100.0, 500.0, 1000.0):
        point = tuple(origin[axis] + direction[axis] * length for axis in range(3))
        projected = project(projections[1], point)
        assert distance_to_line(projected, first, second) < 1e-6


def test_a_ray_segment_can_be_refused_when_it_misses_the_picture():
    projections = fixture_projections()

    # A click in c1's bottom-right corner is still a valid ray; choose an
    # image rectangle far smaller than the projected line's span. The exact
    # line is irrelevant: the helper must return None when it cannot clip.
    segment = epipolar_segment(
        projections[0], (0.0, 0.0), projections[1], 2, 2
    )
    assert segment is None or all(
        0.0 <= value <= 1.0 for endpoint in segment for value in endpoint
    )


def test_clip_segment_clips_to_the_image_edges():
    assert clip_segment((-10.0, 160.0), (330.0, 160.0), 320, 320) == (
        (0.0, 160.0),
        (319.0, 160.0),
    )
    assert clip_segment((40.0, -10.0), (40.0, 400.0), 320, 320) == (
        (40.0, 0.0),
        (40.0, 319.0),
    )


def test_clip_segment_returns_none_when_the_line_misses_the_picture():
    assert clip_segment((-40.0, -40.0), (-10.0, -10.0), 320, 320) is None


def test_clip_segment_refuses_an_empty_image():
    assert clip_segment((0.0, 0.0), (1.0, 1.0), 0, 320) is None
    assert clip_segment((0.0, 0.0), (1.0, 1.0), 320, 0) is None
