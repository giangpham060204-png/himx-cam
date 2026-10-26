"""L1 simulator pin translation must not alter the real config."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from verify_klipper_sim import simulator_config  # noqa: E402


def test_gpio_names_become_numeric():
    text = "step_pin: gpio2\nendstop_pin: ^gpio13\nenable_pin: !gpio6\n"
    assert simulator_config(text) == (
        "step_pin: 2\nendstop_pin: ^13\nenable_pin: !6\n"
    )


def test_non_gpio_text_is_unchanged():
    text = "# gpio13 comment\nserial: /dev/ttyACM0\n"
    assert simulator_config(text) == "# 13 comment\nserial: /dev/ttyACM0\n"
