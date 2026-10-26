"""Config loading and validation for the arm host layer."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.config import load_config  # noqa: E402
from src.errors import ConfigError  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "src" / "config" / "arm.toml"


def _write(tmp_path, text):
    path = tmp_path / "arm.toml"
    path.write_text(text)
    return path


def _default_text():
    return DEFAULT.read_text()


def test_default_config_loads_and_computes_units():
    config = load_config(DEFAULT)

    assert config.stack == "A"
    assert config.home_order == ("m3", "m2", "m1")
    assert config.axes["m1"].steps_per_unit == pytest.approx(400.0)
    assert config.axes["m2"].steps_per_unit == pytest.approx(200 * 16 / 360)
    assert config.axes["m1"].home_target() == pytest.approx(-20.0)
    assert config.axes["m1"].klipper_min == pytest.approx(-21.0)
    assert config.axes["m1"].klipper_max == pytest.approx(221.0)
    assert config.axes["m2"].klipper_min == pytest.approx(-361.0)
    assert config.axes["m2"].klipper_max == pytest.approx(361.0)
    assert config.axes["m3"].klipper_min == pytest.approx(-21.0)
    assert config.axes["m3"].klipper_max == pytest.approx(121.0)


def test_config_hash_is_stable_and_short():
    first = load_config(DEFAULT).config_hash
    second = load_config(DEFAULT).config_hash

    assert first == second
    assert len(first) == 16
    int(first, 16)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.toml")


def test_malformed_toml_is_refused(tmp_path):
    path = _write(tmp_path, "{not toml")

    with pytest.raises(ConfigError, match="valid TOML"):
        load_config(path)


def test_missing_klipper_section_is_refused(tmp_path):
    path = _write(tmp_path, '[robot]\nstack = "A"\n')

    with pytest.raises(ConfigError, match="klipper"):
        load_config(path)


def test_non_integer_full_steps_is_refused(tmp_path):
    text = _default_text().replace(
        "full_steps_per_rev = 200", "full_steps_per_rev = 318.58", 1
    )
    path = _write(tmp_path, text)

    with pytest.raises(ConfigError, match="full_steps_per_rev"):
        load_config(path)


def test_microsteps_must_be_a_power_of_two(tmp_path):
    text = _default_text().replace("microsteps = 16", "microsteps = 12", 1)
    path = _write(tmp_path, text)

    with pytest.raises(ConfigError, match="microsteps"):
        load_config(path)


def test_duplicate_pin_is_refused(tmp_path):
    text = _default_text().replace('step_pin = "gpio7"', 'step_pin = "gpio2"')
    path = _write(tmp_path, text)

    with pytest.raises(ConfigError, match="gpio2"):
        load_config(path)


def test_reserved_pico_pin_is_refused(tmp_path):
    text = _default_text().replace('step_pin = "gpio2"', 'step_pin = "gpio23"')
    path = _write(tmp_path, text)

    with pytest.raises(ConfigError, match="reserved"):
        load_config(path)


def test_m2_beyond_cable_wind_is_refused(tmp_path):
    text = _default_text().replace("min_pos = -360.0", "min_pos = -400.0")
    path = _write(tmp_path, text)

    with pytest.raises(ConfigError, match="m2_cable_wind_deg"):
        load_config(path)


def test_m2_home_target_beyond_cable_wind_is_refused(tmp_path):
    text = _default_text().replace("home_travel = 0.0", "home_travel = 30.0")
    path = _write(tmp_path, text)

    with pytest.raises(ConfigError, match="home_target"):
        load_config(path)


def test_bad_home_order_is_refused(tmp_path):
    text = _default_text().replace(
        'home_order = ["m3", "m2", "m1"]',
        'home_order = ["m1", "m1", "m2"]',
    )
    path = _write(tmp_path, text)

    with pytest.raises(ConfigError, match="home_order"):
        load_config(path)


def test_run_current_above_motor_rating_is_refused(tmp_path):
    text = _default_text().replace("run_current = 1.0", "run_current = 1.5", 1)
    path = _write(tmp_path, text)

    with pytest.raises(ConfigError, match="run_current"):
        load_config(path)
