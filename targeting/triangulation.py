"""Where the rays from several cameras meet, in plain Python.

One point, seen by three cameras, each of which reports the pixel it landed on.
Recovering the point means finding where the three lines of sight come closest
to agreeing.

Two things here are deliberate, and both came out of building the synthetic
camera and watching it misbehave:

* The textbook answer is the linear method, which solves for a homogeneous
  point and divides by its fourth coordinate. That division is where the error
  lives. With the point a metre from the cameras and near the world origin, one
  pixel of detector noise moved the linear answer by 190 mm -- and moving the
  world origin 500 mm, changing nothing else, moved it down to 6 mm. So the
  linear method is not used at all: the starting guess is the closest approach
  of the rays, which cannot run away, and it is then polished by minimising the
  reprojection error over a plain three-dimensional point.

* No numpy. Like the rest of this package, this runs on a bare interpreter and
  inside Slicer. The arithmetic is only 3x3 systems, so that costs little.

Points are three-tuples and projection matrices are 3x4 sequences of rows.
"""

from __future__ import annotations

from math import sqrt

# Below this a pivot is treated as zero: the cameras do not pin the point down.
_TINY = 1e-12


def _solve3(matrix, rhs):
    """Solve a 3x3 system by Gaussian elimination with partial pivoting."""
    rows = [[float(matrix[i][j]) for j in range(3)] + [float(rhs[i])] for i in range(3)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < _TINY:
            raise ValueError("these cameras do not pin the point down: the system is singular")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for row in range(column + 1, 3):
            factor = rows[row][column] / rows[column][column]
            for k in range(column, 4):
                rows[row][k] -= factor * rows[column][k]

    solution = [0.0, 0.0, 0.0]
    for row in (2, 1, 0):
        rest = sum(rows[row][k] * solution[k] for k in range(row + 1, 3))
        solution[row] = (rows[row][3] - rest) / rows[row][row]
    return solution


def _length(vector):
    return sqrt(sum(component * component for component in vector))


def look_at(position, target):
    """The rotation that turns a camera at ``position`` to face ``target``.

    Rows are the camera's own right, down and forward axes, so multiplying a
    world direction by this matrix gives it in camera coordinates. Pointing
    straight up has no well defined "right", so it is refused rather than
    answered with an arbitrary roll.
    """
    forward = [target[i] - position[i] for i in range(3)]
    span = _length(forward)
    if span < _TINY:
        raise ValueError("a camera cannot look at the place it is standing")
    forward = [component / span for component in forward]

    right = [1.0 * forward[2] - 0.0 * forward[1],
             0.0 * forward[0] - 0.0 * forward[2],
             0.0 * forward[1] - 1.0 * forward[0]]
    span = _length(right)
    if span < 1e-9:
        raise ValueError("a camera pointed straight up has no sensible roll")
    right = [component / span for component in right]

    down = [forward[1] * right[2] - forward[2] * right[1],
            forward[2] * right[0] - forward[0] * right[2],
            forward[0] * right[1] - forward[1] * right[0]]
    return (tuple(right), tuple(down), tuple(forward))


def camera_pose(position, target):
    """``(R, t)`` for a camera at ``position`` facing ``target``.

    The translation is whatever puts the camera's own origin at ``position``,
    which is the same statement as the rays starting there.
    """
    R = look_at(position, target)
    t = tuple(-sum(R[i][j] * position[j] for j in range(3)) for i in range(3))
    return R, t


def projection_matrix(K, R, t):
    """The 3x4 matrix that takes a world point to the pixel it lands on."""
    return tuple(
        tuple(sum(K[i][k] * R[k][j] for k in range(3)) for j in range(3))
        + (sum(K[i][k] * t[k] for k in range(3)),)
        for i in range(3)
    )


def project(projection, point):
    """The pixel ``point`` lands on, as seen by ``projection``."""
    u, v, w = _image_coordinates(projection, point)
    if abs(w) < _TINY:
        raise ValueError("that point is on the camera's own plane")
    return (u / w, v / w)


def _image_coordinates(projection, point):
    return tuple(sum(projection[i][k] * point[k] for k in range(3)) + projection[i][3]
                 for i in range(3))


def ray_for(projection, pixel):
    """``(origin, unit direction)`` of the line of sight that pixel saw.

    The camera sits where the projection matrix sends the origin of the world
    to zero, which is a 3x3 solve; the direction is the same matrix applied to
    the pixel, which is why neither needs the heavy machinery of a null space.
    """
    M = tuple(tuple(projection[i][j] for j in range(3)) for i in range(3))
    origin = _solve3(M, [-projection[i][3] for i in range(3)])
    direction = _solve3(M, [pixel[0], pixel[1], 1.0])
    span = _length(direction)
    if span < _TINY:
        raise ValueError("that pixel has no line of sight")
    return tuple(origin), tuple(component / span for component in direction)


def midpoint_point(projections, pixels):
    """Closest approach of the measured rays: a first guess that cannot run away.

    This is Hartley and Sturm's midpoint method. It is one closed-form least
    squares with nothing to divide by, so unlike the linear method it behaves
    the same wherever the world origin happens to be.
    """
    if len(projections) < 2:
        raise ValueError("one camera cannot place a point: at least two are needed")

    normal = [[0.0] * 3 for _ in range(3)]
    offset = [0.0] * 3
    for projection, pixel in zip(projections, pixels):
        origin, direction = ray_for(projection, pixel)
        along = sum(direction[k] * origin[k] for k in range(3))
        for i in range(3):
            offset[i] += origin[i] - direction[i] * along
            for j in range(3):
                normal[i][j] += (1.0 if i == j else 0.0) - direction[i] * direction[j]
    return tuple(_solve3(normal, offset))


def reprojection_cost(projections, pixels, point):
    """Squared pixel distance between where the point lands and where it was seen."""
    total = 0.0
    for projection, (x, y) in zip(projections, pixels):
        u, v, w = _image_coordinates(projection, point)
        if abs(w) < _TINY:
            return float("inf")
        total += (u / w - x) ** 2 + (v / w - y) ** 2
    return total


def refine_point(projections, pixels, point, iterations=60):
    """Gauss-Newton on the reprojection error, over a plain three-dimensional point.

    No homogeneous coordinate, so no division by something small to amplify the
    noise. Steps that would make the reprojection worse are halved instead of
    taken, which is what lets this start from anywhere.
    """
    point = [float(component) for component in point]
    cost = reprojection_cost(projections, pixels, point)

    for _ in range(iterations):
        normal = [[0.0] * 3 for _ in range(3)]
        gradient = [0.0] * 3
        for projection, (x, y) in zip(projections, pixels):
            u, v, w = _image_coordinates(projection, point)
            rows = (((projection[0][k] * w - u * projection[2][k]) / (w * w) for k in range(3)),
                    ((projection[1][k] * w - v * projection[2][k]) / (w * w) for k in range(3)))
            for row, residual in zip(rows, (u / w - x, v / w - y)):
                row = tuple(row)
                for i in range(3):
                    gradient[i] += row[i] * residual
                    for j in range(3):
                        normal[i][j] += row[i] * row[j]

        step = _solve3(normal, [-value for value in gradient])
        if any(value != value or value in (float("inf"), float("-inf")) for value in step):
            break

        for shrink in (1.0, 0.5, 0.25, 0.125, 0.0625):
            candidate = [point[i] + shrink * step[i] for i in range(3)]
            candidate_cost = reprojection_cost(projections, pixels, candidate)
            if candidate_cost <= cost:
                point, cost = candidate, candidate_cost
                break
        else:
            break
        if _length([shrink * value for value in step]) < 1e-10:
            break

    return tuple(point)


def triangulate(projections, pixels):
    """The point those cameras agree on: closest approach, then polished."""
    return refine_point(projections, pixels, midpoint_point(projections, pixels))
