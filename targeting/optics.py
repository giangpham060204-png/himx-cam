"""The optics of the HM01B0 camera modules we can actually buy.

The synthetic camera and every accuracy figure need a lens and a sensor. Both
live here, once, as data -- because the two documented modules differ by a
factor of 2.2 in focal length, and that difference moves every millimetre
figure in the client brief. A hardcoded focal length somewhere else would make
one of the two modules quietly wrong.

Like the rest of this package, no numpy: plain data and plain arithmetic, so
the numbers can be read from anywhere.

Sources, both fetched and read directly:

* Arducam HM01B0 module datasheet, "Module specification" table --
  sensor HM01B0-MWA, EFL 1.47 mm, F/No 2.8 +/- 5%, view angle 60 degrees,
  distortion < 1.0%, and AFVDD = NC: there is no focus motor.
* Himax HM01B0-MNA-01FT870 datasheet, section 1.3 "Key parameters" --
  sensor HM01B0-MNA, EFL 0.66 mm +/- 5%, F/No 2.4, image circle 1.83 mm,
  BFL 1.04 mm, TV distortion under 4.3%, field of view 115 degrees diagonal.
  Its focus distance of 35 cm and depth of field of 30 cm to infinity are
  printed on the Bitcraze AI-deck camera datasheet for the same module.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan, degrees, hypot


@dataclass(frozen=True)
class Optics:
    """One lens on one sensor, with the numbers taken from its datasheet.

    Distances are metres. ``focus_distance`` is ``None`` where the maker does
    not publish one, which is not the same as "focused at zero" -- callers
    that need a focus distance must decide what to assume.
    """

    name: str
    sensor: str
    pixels_x: int
    pixels_y: int
    pixel_size: float
    focal_length: float
    f_number: float
    distortion: float
    published_fov_deg: float
    focus_distance: float | None
    source: str

    @property
    def diagonal(self) -> float:
        """The imaged diagonal, set by the sensor and not by the lens."""
        return hypot(self.pixels_x, self.pixels_y) * self.pixel_size

    @property
    def focal_length_pixels(self) -> float:
        """The focal length in pixels: the scale that turns pixels into angles."""
        return self.focal_length / self.pixel_size

    @property
    def angular_resolution(self) -> float:
        """The angle one pixel spans, in radians."""
        return self.pixel_size / self.focal_length

    @property
    def implied_fov_deg(self) -> float:
        """The diagonal field of view the focal length and sensor imply."""
        return 2.0 * degrees(atan(self.diagonal / 2.0 / self.focal_length))

    def pixel_footprint(self, distance: float) -> float:
        """How much of the world one pixel covers at ``distance``, in metres."""
        return self.angular_resolution * distance


MODULES: dict[str, Optics] = {
    "arducam-hm01b0": Optics(
        name="Arducam HM01B0 monochrome module for Pico",
        sensor="HM01B0-MWA",
        pixels_x=320,
        pixels_y=320,
        pixel_size=3.6e-6,
        focal_length=1.47e-3,
        f_number=2.8,
        distortion=0.01,
        published_fov_deg=60.0,
        focus_distance=None,
        source="Arducam HM01B0 datasheet, 'Module specification' table",
    ),
    "himax-hm01b0-mna": Optics(
        name="Himax HM01B0-MNA compact camera module",
        sensor="HM01B0-MNA",
        pixels_x=320,
        pixels_y=320,
        pixel_size=3.6e-6,
        focal_length=0.66e-3,
        f_number=2.4,
        distortion=0.043,
        published_fov_deg=115.0,
        focus_distance=0.35,
        source="HM01B0-MNA-01FT870 datasheet, section 1.3",
    ),
}

# The one the client can buy today, and whose datasheet agrees with itself.
DEFAULT_MODULE = "arducam-hm01b0"


def optics_for(name: str = DEFAULT_MODULE) -> Optics:
    """The named module, or a refusal naming the ones that exist."""
    try:
        return MODULES[name]
    except KeyError:
        raise ValueError(f"unknown camera module {name!r}; known modules are {sorted(MODULES)}") from None
