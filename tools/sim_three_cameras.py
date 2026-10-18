"""Visual simulation of the 3-camera trilateration scheme.

Three cameras at known positions measure their distance to one target point;
the point is recovered as the intersection of the three spheres (GPS-style).

Usage:
    /tmp/viz/bin/python tools/sim_three_cameras.py \
        --target 60 40 120 --noise 2.0 --out /tmp/three_cam_sim.png

Arguments are millimetres. --noise is the per-camera distance error (stddev)
used to show what happens with imperfect ranging. Prints a numeric report
and saves a 3D figure: camera pyramids, range spheres, true target (green
star) vs recovered estimate (red X).
"""

import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401  (registers 3D projection)

# Fixed camera bases, mm. Spread in 3D on purpose: near-coplanar
# layouts make the height solution unstable (try C3 at z=60 and watch
# a ±2mm ranging error blow up into tens of mm).
CAMS = np.array(
    [
        [0.0, 0.0, 0.0],
        [240.0, 20.0, 170.0],
        [20.0, 240.0, 60.0],
    ]
)


def trilaterate(cams, dists, prefer="high"):
    """Intersection of three range spheres (least-squares + quadratic).

    Subtracting sphere equations linearizes to 2 equations in 3 unknowns
    (a line); substituting back into one sphere equation gives a
    quadratic with 0-2 roots. `prefer="high"` picks the root above the
    cameras' plane (the table constraint: targets live above the table).
    """
    A = 2 * (cams[1:] - cams[0])
    b = (dists[0] ** 2 - dists[1:] ** 2) + (
        np.sum(cams[1:] ** 2, axis=1) - np.sum(cams[0] ** 2)
    )
    p0, *_ = np.linalg.lstsq(A, b, rcond=None)
    n = np.cross(A[0], A[1])
    norm = np.linalg.norm(n)
    if norm < 1e-9:
        raise ValueError("camera bases are collinear: no unique solution")
    n = n / norm
    w = p0 - cams[0]
    bq = 2 * float(w @ n)
    c = float(w @ w - dists[0] ** 2)
    disc = bq * bq - 4 * c
    if disc < 0:
        raise ValueError(f"spheres do not intersect (disc={disc:.2f})")
    s = np.sqrt(disc)
    roots = [p0 + t * n for t in ((-bq + s) / 2, (-bq - s) / 2)]
    if prefer == "high":
        return max(roots, key=lambda p: p[2])
    return min(roots, key=lambda p: p[2])


def draw_camera(ax, pos, label, reach=45.0):
    """A small pyramid glyph pointing +Z from the camera centre."""
    ax.scatter(*pos, c="k", s=40)
    ax.text(pos[0], pos[1], pos[2] + 8, label, fontsize=10)
    base = np.array(
        [
            pos + [-reach / 2, -reach / 2, reach],
            pos + [reach / 2, -reach / 2, reach],
            pos + [reach / 2, reach / 2, reach],
            pos + [-reach / 2, reach / 2, reach],
            pos + [-reach / 2, -reach / 2, reach],
        ]
    )
    ax.plot(base[:, 0], base[:, 1], base[:, 2], c="k", lw=1)
    for corner in base[:4]:
        ax.plot([pos[0], corner[0]], [pos[1], corner[1]], [pos[2], corner[2]], c="k", lw=0.7)


def draw_sphere(ax, center, radius, color):
    u = np.linspace(0, 2 * np.pi, 24)
    v = np.linspace(0, np.pi, 12)
    x = center[0] + radius * np.outer(np.cos(u), np.sin(v))
    y = center[1] + radius * np.outer(np.sin(u), np.sin(v))
    z = center[2] + radius * np.outer(np.ones_like(u), np.cos(v))
    ax.plot_wireframe(x, y, z, color=color, alpha=0.25, rstride=3, cstride=3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, nargs=3, default=[90.0, 70.0, 130.0])
    ap.add_argument("--noise", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="/tmp/three_cam_sim.png")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    truth = np.array(args.target)
    true_d = np.linalg.norm(CAMS - truth, axis=1)
    meas_d = true_d + rng.normal(0.0, args.noise, size=3)
    est = trilaterate(CAMS, meas_d)
    err = float(np.linalg.norm(est - truth))

    print(f"target (truth)      : {truth}")
    print(f"true distances      : {np.round(true_d, 2)}")
    print(f"measured (+noise)   : {np.round(meas_d, 2)}")
    print(f"recovered estimate  : {np.round(est, 2)}")
    print(f"error               : {err:.2f} mm")

    fig = plt.figure(figsize=(9, 7))
    ax = fig.add_subplot(111, projection="3d")
    colors = ["tab:blue", "tab:orange", "tab:green"]
    for i, (cam, color) in enumerate(zip(CAMS, colors)):
        draw_camera(ax, cam, f"C{i + 1}")
        draw_sphere(ax, cam, meas_d[i], color)
        ax.plot(
            [cam[0], truth[0]], [cam[1], truth[1]], [cam[2], truth[2]],
            c=color, lw=1.2, ls="--",
        )
    t = tuple(round(float(v), 1) for v in truth)
    ax.scatter(*truth, c="g", s=120, marker="*", label=f"truth {t}")
    ax.scatter(*est, c="r", s=100, marker="X", label=f"estimate err {err:.1f}mm")
    ax.plot(
        [truth[0], est[0]], [truth[1], est[1]], [truth[2], est[2]],
        c="r", lw=2,
    )
    ax.set_xlabel("X mm")
    ax.set_ylabel("Y mm")
    ax.set_zlabel("Z mm")
    ax.set_title(f"3-camera trilateration (range noise ±{args.noise}mm)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(args.out, dpi=110)
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
