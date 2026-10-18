"""Tests for finding the point three cameras agree on.

Plain Python, like the rest of this package. The behaviour pinned here is the
behaviour the draft exposed the hard way:

* the answer must not depend on where the world origin happens to be -- the
  textbook linear method does, by a factor of thirty, which is a silent way to
  be wrong;
* a pixel of detector noise must translate into millimetres, not centimetres;
* the error must grow with the square of the distance and fall as the cameras
  move apart, because those two laws are what the accuracy table promises.
"""

import pathlib
import random
import sys
from math import cos, radians, sin, sqrt

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.optics import optics_for  # noqa: E402
from targeting.triangulation import (  # noqa: E402
    camera_pose,
    look_at,
    midpoint_point,
    project,
    projection_matrix,
    ray_for,
    refine_point,
    reprojection_cost,
    triangulate,
)

OPTICS = optics_for()


def camera_matrix(optics=OPTICS):
    f = optics.focal_length_pixels
    return [[f, 0.0, optics.pixels_x / 2.0],
            [0.0, f, optics.pixels_y / 2.0],
            [0.0, 0.0, 1.0]]


def rig(spread, distance, centre=(0.0, 0.0, 0.0)):
    """Three cameras on a ring around the point, at slightly different heights."""
    positions = []
    for angle, height in zip((90.0, 210.0, 330.0), (0.0, 60.0, 30.0)):
        positions.append((centre[0] + spread * cos(radians(angle)),
                          centre[1] + spread * sin(radians(angle)),
                          centre[2] + distance + height))
    return positions


def setup(spread, distance, centre=(0.0, 0.0, 0.0), optics=OPTICS):
    """Projection matrices for that rig, all looking at ``centre``."""
    K = camera_matrix(optics)
    projections = []
    for position in rig(spread, distance, centre):
        R = look_at(position, centre)
        t = [-sum(R[i][j] * position[j] for j in range(3)) for i in range(3)]
        projections.append(projection_matrix(K, R, t))
    return projections, centre


def jitter(pixels, sigma, seed=0):
    generator = random.Random(seed)
    return [(x + generator.gauss(0.0, sigma), y + generator.gauss(0.0, sigma))
            for x, y in pixels]


def distance_between(a, b):
    return sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def _root_mean_square(values):
    values = list(values)
    return sqrt(sum(value * value for value in values) / len(values))


def setup_aimed(spread, standoff, aim, optics=OPTICS):
    """Projection matrices for a rig aimed at ``aim``, while the point stays at the origin."""
    K = camera_matrix(optics)
    projections = []
    for position in rig(spread, standoff):
        R = look_at(position, aim)
        t = [-sum(R[i][j] * position[j] for j in range(3)) for i in range(3)]
        projections.append(projection_matrix(K, R, t))
    return projections, (0.0, 0.0, 0.0)


def rms_error(solver, projections, truth, sigma, trials=60):
    total = 0.0
    for seed in range(trials):
        pixels = jitter([project(projection, truth) for projection in projections], sigma, seed)
        total += distance_between(solver(projections, pixels), truth) ** 2
    return sqrt(total / trials)


def parallel_pair(baseline, distance):
    """Two cameras side by side, both looking straight ahead down +z.

    The textbook stereo pair, where the depth error has a closed form and can be
    checked against the simulation rather than merely watched.
    """
    K = camera_matrix()
    projections = []
    for side in (-1.0, 1.0):
        position = (side * baseline / 2.0, 0.0, 0.0)
        R = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        projections.append(projection_matrix(K, R, tuple(-c for c in position)))
    return projections, (0.0, 0.0, distance)


def stumble(projections, truth, sigma, seed=0):
    """Triangulate a jittered look at ``truth``, and report how far off it lands."""
    pixels = [project(projection, truth) for projection in projections]
    return distance_between(triangulate(projections, jitter(pixels, sigma, seed)), truth)


# --- pointing a camera ----------------------------------------------------


def test_look_at_turns_the_camera_towards_its_target():
    R = look_at((0.0, 100.0, 500.0), (0.0, 0.0, 0.0))

    # the world direction from camera to target must come out as the camera's z
    forward = (-0.0, -100.0, -500.0)
    length = sqrt(sum(c * c for c in forward))
    turned = [sum(R[i][j] * forward[j] / length for j in range(3)) for i in range(3)]
    assert turned[0] == pytest.approx(0.0, abs=1e-12)
    assert turned[1] == pytest.approx(0.0, abs=1e-12)
    assert turned[2] == pytest.approx(1.0, abs=1e-12)


def test_look_at_produces_a_rotation_not_a_reflection():
    R = look_at((150.0, -80.0, 620.0), (10.0, 5.0, 0.0))
    determinant = (R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1])
                   - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
                   + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0]))
    assert determinant == pytest.approx(1.0, abs=1e-12)


def test_look_at_refuses_a_camera_that_would_have_to_guess_its_roll():
    # looking along the very axis used as "up" leaves no way to choose a roll,
    # so it must complain rather than quietly return a camera pointing anywhere
    with pytest.raises(ValueError, match="no sensible roll"):
        look_at((0.0, -500.0, 100.0), (0.0, 0.0, 100.0))


def test_a_camera_looking_at_a_point_puts_it_in_the_centre_of_its_picture():
    K = camera_matrix()
    position = (0.0, 150.0, 500.0)
    R = look_at(position, (0.0, 0.0, 0.0))
    t = [-sum(R[i][j] * position[j] for j in range(3)) for i in range(3)]

    u, v = project(projection_matrix(K, R, t), (0.0, 0.0, 0.0))

    assert u == pytest.approx(OPTICS.pixels_x / 2.0, abs=1e-9)
    assert v == pytest.approx(OPTICS.pixels_y / 2.0, abs=1e-9)


# --- the ray behind a pixel ----------------------------------------------


def test_camera_pose_puts_the_camera_where_its_own_rays_start():
    position, target = (120.0, -90.0, 640.0), (0.0, 0.0, 0.0)

    R, t = camera_pose(position, target)
    projection = projection_matrix(camera_matrix(), R, t)
    origin, _ = ray_for(projection, project(projection, (10.0, -5.0, 20.0)))

    assert distance_between(origin, position) < 1e-9


def test_the_ray_behind_a_pixel_leaves_the_camera_towards_the_point():
    K = camera_matrix()
    position = (100.0, -150.0, 700.0)
    R = look_at(position, (0.0, 0.0, 0.0))
    t = [-sum(R[i][j] * position[j] for j in range(3)) for i in range(3)]
    projection = projection_matrix(K, R, t)
    somewhere = (5.0, -8.0, 40.0)

    origin, direction = ray_for(projection, project(projection, somewhere))

    assert distance_between(origin, position) < 1e-9
    expected = [(somewhere[i] - position[i]) for i in range(3)]
    length = sqrt(sum(c * c for c in expected))
    for i in range(3):
        assert direction[i] == pytest.approx(expected[i] / length, abs=1e-9)


# --- where the rays meet --------------------------------------------------


def test_rays_that_meet_exactly_are_solved_exactly():
    projections, truth = setup(150.0, 500.0)
    pixels = [project(projection, truth) for projection in projections]

    found = midpoint_point(projections, pixels)

    assert distance_between(found, truth) < 1e-9


def test_the_answer_does_not_depend_on_where_the_world_origin_sits():
    """The trap the draft found: the linear method's error tracked the origin."""
    sigma, seed = 0.3, 11
    near = stumble(*setup(150.0, 500.0, centre=(0.0, 0.0, 0.0)), sigma, seed)
    far = stumble(*setup(150.0, 500.0, centre=(500.0, 500.0, 500.0)), sigma, seed)

    # same geometry, same noise, same answer -- to well inside a hundredth of a
    # millimetre, where the linear method moved by thirty times more than this
    assert far == pytest.approx(near, abs=0.01)


def test_refining_never_leaves_the_reprojection_worse_than_it_found_it():
    projections, truth = setup(150.0, 500.0)
    pixels = jitter([project(projection, truth) for projection in projections], 0.5, seed=3)

    start = midpoint_point(projections, pixels)
    polished = refine_point(projections, pixels, start)

    assert reprojection_cost(projections, pixels, polished) <= reprojection_cost(
        projections, pixels, start)


def test_refining_climbs_out_of_a_deliberately_awful_starting_guess():
    projections, truth = setup(150.0, 500.0)
    pixels = [project(projection, truth) for projection in projections]

    found = refine_point(projections, pixels, (-800.0, 900.0, -700.0))

    assert distance_between(found, truth) < 1e-6


# --- the two laws the accuracy table promises ----------------------------


def test_error_grows_with_the_square_of_the_distance():
    sigma, trials = 0.3, 60
    near = _root_mean_square(stumble(*setup(150.0, 250.0), sigma, seed) for seed in range(trials))
    far = _root_mean_square(stumble(*setup(150.0, 500.0), sigma, seed) for seed in range(trials))

    # the cameras sit 150 mm off to the side, so doubling the standoff takes the
    # distance to the point from 291 mm to 522 mm -- a factor of 1.79, squared
    assert 2.0 < far / near < 4.5


def test_the_depth_error_follows_the_textbook_law():
    """sigma_z = sqrt(2) * sigma * z^2 / (focal length * baseline).

    Two cameras side by side looking straight ahead is the case where the law is
    exact, so this is the place to check the number the accuracy figure promises
    rather than merely that it grows.
    """
    sigma, baseline, trials = 0.3, 200.0, 60
    for distance in (300.0, 600.0):
        projections, truth = parallel_pair(baseline, distance)
        measured = _root_mean_square(stumble(projections, truth, sigma, seed)
                                     for seed in range(trials))
        predicted = (sqrt(2.0) * sigma * distance ** 2
                     / (OPTICS.focal_length_pixels * baseline))
        assert measured == pytest.approx(predicted, rel=0.2)


def test_error_falls_as_the_cameras_move_apart():
    sigma, trials = 0.3, 60
    tight = _root_mean_square(stumble(*setup(80.0, 500.0), sigma, seed) for seed in range(trials))
    wide = _root_mean_square(stumble(*setup(320.0, 500.0), sigma, seed) for seed in range(trials))

    # four times the spread, so about a quarter of the error
    assert tight / wide > 2.0


def test_a_pixel_of_noise_costs_millimetres_not_centimetres():
    projections, truth = setup(150.0, 500.0)
    clean = triangulate(projections, [project(projection, truth) for projection in projections])
    pixels = [project(projection, truth) for projection in projections]
    moved = distance_between(
        triangulate(projections, [(x + 1.0, y) for x, y in pixels]), clean)

    assert moved < 10.0


def test_cameras_that_give_no_answer_are_refused():
    projections, truth = setup(150.0, 500.0)
    pixels = [project(projection, truth) for projection in projections]
    blind = [projections[0], projections[0]]

    with pytest.raises(ValueError):
        midpoint_point(blind, pixels[:2])


# --- what the polishing step is actually worth ---------------------------


def test_closest_approach_is_already_near_best_when_the_cameras_face_the_object():
    """Recorded so nobody reads it as "the polish does nothing" and deletes it.

    On a rig aimed at its object every camera sees it near the middle of the
    picture, where the closest-approach metric and the reprojection error agree
    to a fraction of a percent. The next test is the case that differs.
    """
    projections, truth = setup(150.0, 500.0)

    plain = rms_error(midpoint_point, projections, truth, 0.3)
    polished = rms_error(triangulate, projections, truth, 0.3)

    assert polished / plain > 0.95


def test_refining_earns_its_keep_on_a_steeply_oblique_view():
    """The case it exists for: a camera looking sideways at the object.

    A rig spread across a workspace puts its object well off to one side of at
    least one camera. There the closest-approach metric is the wrong one -- it
    measures 3D distances, not the pixels the noise actually lives in -- and
    minimising the reprojection error is worth about a quarter of the error.
    """
    projections, truth = setup_aimed(150.0, 300.0, (400.0, 400.0, 0.0))

    plain = rms_error(midpoint_point, projections, truth, 0.3)
    polished = rms_error(triangulate, projections, truth, 0.3)

    assert polished < 0.9 * plain
    assert reprojection_cost(projections, [
        project(projection, truth) for projection in projections],
        midpoint_point(projections, [project(projection, truth) for projection in projections])
    ) > 0.0
