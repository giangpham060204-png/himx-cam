"""Plain-language visual explainer for the 3-camera ranging spec.

Produces one 4-panel figure that explains, in order:
  A. top-down: each camera gives a circle of possible places; they cross at the target
  B. side view: three distances give TWO answers; we keep the one above the table
  C. thin lens: f, do, di and the formula the client quoted
  D. depth of field: why focus ranging only works close up

Usage:
    /tmp/viz/bin/python tools/explain_camera_maths.py --out docs/figures/camera-maths-explained.png
"""

import argparse
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

C_CAM = "tab:blue"
C_TARGET = "tab:green"
C_NOTE = "tab:red"
C_SHARP = "tab:orange"


def panel_a_trilateration(ax):
    """Top-down: three cameras, three circles, one crossing point."""
    cams = np.array([[0.0, 0.0], [220.0, 20.0], [40.0, 220.0]])
    target = np.array([100.0, 100.0])

    for i, cam in enumerate(cams, start=1):
        r = float(np.linalg.norm(target - cam))
        ax.add_patch(plt.Circle(cam, r, fill=False, ls="--", lw=1.2, color=C_CAM, alpha=0.8))
        ax.plot(*cam, marker="^", ms=13, color=C_CAM)
        ax.text(cam[0] - 6, cam[1] - 22, f"Cam {i}", color=C_CAM, fontsize=10, ha="center")
        ax.plot([cam[0], target[0]], [cam[1], target[1]], color=C_CAM, lw=0.8, alpha=0.5)
        mid = (cam + target) / 2
        ax.text(mid[0] + 6, mid[1], f"{r:.0f} mm", color=C_CAM, fontsize=8, alpha=0.9)

    ax.plot(*target, marker="*", ms=22, color=C_TARGET, zorder=5)
    ax.text(target[0] + 12, target[1] + 4, "target\n(all three agree here)",
            color=C_TARGET, fontsize=10)

    ax.set_title("A. Top-down: 3 distances = 3 circles\none point satisfies all three",
                 fontsize=11)
    ax.set_xlabel("sideways (mm)")
    ax.set_ylabel("forwards (mm)")
    ax.set_xlim(-40, 330)
    ax.set_ylim(-40, 330)
    ax.set_aspect("equal")
    ax.grid(alpha=0.2)


def panel_b_two_solutions(ax):
    """Side view: two circles cross at two points; keep the upper one."""
    c1, r1 = np.array([0.0, 0.0]), 150.0
    c2, r2 = np.array([200.0, 0.0]), 150.0
    x = (r1 ** 2 - r2 ** 2 + c2[0] ** 2 - c1[0] ** 2) / (2 * (c2[0] - c1[0]))
    y = float(np.sqrt(max(r1 ** 2 - (x - c1[0]) ** 2, 0.0)))

    for c, r, label in ((c1, r1, "Cam 1"), (c2, r2, "Cam 2")):
        ax.add_patch(plt.Circle(c, r, fill=False, ls="--", lw=1.2, color=C_CAM, alpha=0.8))
        ax.plot(*c, marker="^", ms=13, color=C_CAM)
        ax.text(c[0], c[1] - 26, label, color=C_CAM, fontsize=10, ha="center")

    ax.axhline(0, color="k", lw=2)
    ax.text(-30, 8, "table / camera plane", fontsize=9)

    ax.plot(x, y, marker="*", ms=22, color=C_TARGET, zorder=5)
    ax.annotate("KEEP this one\n(above the table)",
                xy=(x, y), xytext=(x + 30, y + 30), color=C_TARGET, fontsize=10,
                arrowprops=dict(arrowstyle="->", color=C_TARGET))
    ax.plot(x, -y, marker="x", ms=13, mew=3, color=C_NOTE, zorder=5)
    ax.annotate("discard this one\n(below the table, impossible)",
                xy=(x, -y), xytext=(x + 30, -y - 40), color=C_NOTE, fontsize=10,
                arrowprops=dict(arrowstyle="->", color=C_NOTE))

    ax.set_title("B. Side view: 3 distances give TWO answers\nthe maths keeps the one above the table",
                 fontsize=11)
    ax.set_xlabel("sideways (mm)")
    ax.set_ylabel("up (mm)")
    ax.set_xlim(-80, 330)
    ax.set_ylim(-220, 220)
    ax.set_aspect("equal")
    ax.grid(alpha=0.2)


def panel_c_thin_lens(ax):
    """The client's formula, drawn."""
    ax.set_xlim(-1.6, 8.6)
    ax.set_ylim(-2.6, 2.6)
    ax.axis("off")

    x_obj, x_lens, x_sensor = 0.0, 5.0, 6.9
    h = 1.5

    # object (an up arrow)
    ax.plot([x_obj, x_obj], [0, h], color="k", lw=2)
    ax.plot([x_obj - 0.12, x_obj, x_obj + 0.12], [h - 0.18, h, h - 0.18], color="k", lw=2)
    ax.text(x_obj, h + 0.22, "object\n(at distance do)", ha="center", fontsize=9)

    # lens (biconvex shape)
    ys = np.linspace(-1.9, 1.9, 200)
    bulge = 0.26 * np.cos(np.pi * ys / (2 * 1.9))
    ax.plot(x_lens + bulge, ys, color="k", lw=1.6)
    ax.plot(x_lens - bulge, ys, color="k", lw=1.6)
    ax.text(x_lens, 2.05, "lens\n(fixed property f)", ha="center", fontsize=9)

    # sensor
    ax.plot([x_sensor, x_sensor], [-1.9, 1.9], color="k", lw=3)
    ax.text(x_sensor + 0.15, 2.05, "sensor\n(320x320 px)", ha="left", fontsize=9)

    # rays: from object tip, through lens, converge on the sensor
    for y_lens in (h, 0.0, -h):
        ax.plot([x_obj, x_lens], [h, y_lens], color="tab:red", lw=0.9, alpha=0.8)
        ax.plot([x_lens, x_sensor], [y_lens, -h], color="tab:red", lw=0.9, alpha=0.8)

    # dimension lines
    ax.annotate("", xy=(x_obj, -2.15), xytext=(x_lens, -2.15),
                arrowprops=dict(arrowstyle="<->", color=C_CAM))
    ax.text((x_obj + x_lens) / 2, -2.5, "do = distance to object\n(WHAT WE WANT)",
            ha="center", color=C_CAM, fontsize=9)

    ax.annotate("", xy=(x_lens, -1.35), xytext=(x_sensor, -1.35),
                arrowprops=dict(arrowstyle="<->", color=C_SHARP))
    ax.text((x_lens + x_sensor) / 2, -1.05, "di\n(lens moves,\nwe read it)",
            ha="center", color=C_SHARP, fontsize=8)

    ax.text(3.2, -0.05,
            r"$\dfrac{1}{f}=\dfrac{1}{d_o}+\dfrac{1}{d_i}$" + "\n\n" +
            r"$d_o=\dfrac{f\cdot d_i}{d_i-f}$",
            fontsize=13, ha="center", va="center",
            bbox=dict(boxstyle="round", fc="white", ec="0.7"))
    ax.set_title("C. The client's formula\nmove the lens until sharp; that tells you the distance",
                 fontsize=11)


def panel_d_depth_of_field(ax):
    """Why focus ranging dies at range: everything looks sharp at once."""
    ax.set_xlim(-0.1, 2.6)
    ax.set_ylim(-1.6, 1.9)
    ax.axis("off")

    ax.annotate("", xy=(2.5, 0), xytext=(0, 0),
                arrowprops=dict(arrowstyle="->", color="k", lw=1.5))
    ax.text(2.52, 0, "distance\nfrom camera (m)", va="center", fontsize=9)
    for d in (0, 0.5, 1.0, 1.5, 2.0, 2.5):
        ax.plot([d, d], [-0.06, 0.06], color="k")
        ax.text(d, -0.22, f"{d:g}", ha="center", fontsize=8)

    ax.axvspan(0.15, 0.5, color=C_SHARP, alpha=0.25)
    ax.text(0.32, 0.62, "SHARP ZONE\n(everything in here\nlooks equally sharp)",
            ha="center", fontsize=9, color="darkorange")

    ax.text(0.32, -0.95, "object here:\nfocus tells you NOTHING\n(inside the sharp zone)",
            ha="center", fontsize=9, color=C_NOTE)
    ax.annotate("", xy=(0.32, -0.1), xytext=(0.32, -0.78),
                arrowprops=dict(arrowstyle="->", color=C_NOTE))

    ax.text(1.65, 0.62, "object here:\nthe sharpest focus is a\nsharp, findable peak",
            ha="center", fontsize=9, color=C_TARGET)
    ax.annotate("", xy=(1.65, 0.1), xytext=(1.65, 0.45),
                arrowprops=dict(arrowstyle="->", color=C_TARGET))

    ax.text(1.25, 1.45, "This is why Path F (focus) is close-range,\n"
                        "and Path M (marker size) covers the rest.",
            ha="center", fontsize=10, style="italic")

    ax.set_title("D. Depth of field\nwhy 'focus until sharp' has a distance limit", fontsize=11)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="images/camera-maths-explained.png")
    args = ap.parse_args()

    fig, axes = plt.subplots(2, 2, figsize=(13, 11))
    panel_a_trilateration(axes[0][0])
    panel_b_two_solutions(axes[0][1])
    panel_c_thin_lens(axes[1][0])
    panel_d_depth_of_field(axes[1][1])

    fig.suptitle("How 3 cameras find a point  —  plain-language explainer",
                 fontsize=14, y=0.985)
    fig.tight_layout(rect=(0, 0, 1, 0.965))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
