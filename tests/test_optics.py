"""Tests for the camera optics table.

Every millimetre figure in the client brief traces back to these numbers, and
the two documented HM01B0 modules differ by a factor of 2.2 in focal length.
So a lens parameter that quietly drifted would silently change the project's
accuracy claims, without changing anything that looks wrong. They are pinned
here against the two datasheets they were read from.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from targeting.optics import DEFAULT_MODULE, MODULES, optics_for  # noqa: E402


# --- the two modules, as their datasheets print them ----------------------


def test_the_default_is_the_module_the_client_can_actually_buy():
    module = optics_for()

    assert DEFAULT_MODULE == "arducam-hm01b0"
    assert module.sensor == "HM01B0-MWA"
    assert module.focal_length == pytest.approx(1.47e-3)
    assert module.f_number == pytest.approx(2.8)
    assert module.published_fov_deg == pytest.approx(60.0)
    assert module.distortion == pytest.approx(0.01)


def test_the_arducam_module_publishes_no_focus_distance():
    # so it must not inherit the other module's 35 cm by accident
    assert optics_for("arducam-hm01b0").focus_distance is None


def test_the_mna_module_carries_its_datasheet_numbers():
    module = optics_for("himax-hm01b0-mna")

    assert module.sensor == "HM01B0-MNA"
    assert module.focal_length == pytest.approx(0.66e-3)
    assert module.f_number == pytest.approx(2.4)
    assert module.published_fov_deg == pytest.approx(115.0)
    assert module.distortion == pytest.approx(0.043)
    assert module.focus_distance == pytest.approx(0.35)


def test_both_modules_sit_on_the_same_sensor():
    for module in MODULES.values():
        assert (module.pixels_x, module.pixels_y) == (320, 320)
        assert module.pixel_size == pytest.approx(3.6e-6)


def test_every_module_is_reachable_by_its_own_key():
    for name, module in MODULES.items():
        assert optics_for(name) is module


def test_an_unknown_module_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="arducam-hm01b0"):
        optics_for("hm01b0")


# --- the derived numbers the reports and the synthetic camera use ---------


def test_focal_length_in_pixels_is_the_scale_everything_else_hangs_on():
    assert optics_for("arducam-hm01b0").focal_length_pixels == pytest.approx(408.333, abs=1e-3)
    assert optics_for("himax-hm01b0-mna").focal_length_pixels == pytest.approx(183.333, abs=1e-3)


def test_a_pixel_spans_a_twice_as_wide_angle_through_the_mna_lens():
    arducam = optics_for("arducam-hm01b0").angular_resolution
    mna = optics_for("himax-hm01b0-mna").angular_resolution

    assert arducam == pytest.approx(2.44898e-3, rel=1e-5)
    assert mna == pytest.approx(5.45455e-3, rel=1e-5)
    # exactly the ratio of the focal lengths, which is the whole point
    assert mna / arducam == pytest.approx(1.47 / 0.66, rel=1e-9)


def test_pixel_footprint_at_one_metre_matches_the_brief():
    # the brief's "1 pixel = 5.45 mm at 1 m" was computed from the MNA lens
    assert optics_for("himax-hm01b0-mna").pixel_footprint(1.0) == pytest.approx(5.4545e-3, rel=1e-5)
    assert optics_for("arducam-hm01b0").pixel_footprint(1.0) == pytest.approx(2.4490e-3, rel=1e-5)
    assert optics_for("arducam-hm01b0").pixel_footprint(2.0) == pytest.approx(4.8980e-3, rel=1e-5)


def test_the_imaged_diagonal_comes_from_the_sensor_not_the_lens():
    for module in MODULES.values():
        assert module.diagonal == pytest.approx(1.6292e-3, rel=1e-4)


def test_the_arducam_field_of_view_agrees_with_its_focal_length():
    module = optics_for("arducam-hm01b0")

    assert module.implied_fov_deg == pytest.approx(58.0, abs=0.5)
    assert abs(module.implied_fov_deg - module.published_fov_deg) < 3.0


def test_the_mna_datasheet_disagrees_with_itself_about_field_of_view():
    """A finding, pinned so the number is not quietly "corrected".

    A 0.66 mm lens on a 1.63 mm sensor spans about 102 degrees, but the
    datasheet prints 115 -- which would need a 0.52 mm lens. The Arducam
    module's own pair agree with each other, and that is part of why it is
    the default.
    """
    module = optics_for("himax-hm01b0-mna")

    assert module.implied_fov_deg == pytest.approx(102.0, abs=0.5)
    assert module.implied_fov_deg - module.published_fov_deg == pytest.approx(-13.0, abs=1.0)
