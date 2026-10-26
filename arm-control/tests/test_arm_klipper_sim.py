"""Optional Klipper simulator test.

Set KLIPPER_DIR to a Klipper checkout that has been built with the host
simulator target and contains out/klipper.dict.
"""

import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
KLIPPER = os.environ.get("KLIPPER_DIR")

pytestmark = pytest.mark.skipif(
    not KLIPPER, reason="set KLIPPER_DIR to a built Klipper checkout"
)


def test_verify_klipper_sim_script(tmp_path):
    if not (pathlib.Path(KLIPPER) / "out" / "klipper.dict").is_file():
        pytest.skip("Klipper has no out/klipper.dict; build the simulator target")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "verify_klipper_sim.py"),
            "--klipper-dir",
            KLIPPER,
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "queue_step" in result.stdout
