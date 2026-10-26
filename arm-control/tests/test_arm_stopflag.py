"""Cross-process controlled-stop flag tests."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from arm_control.stopflag import StopFlag  # noqa: E402


def test_request_and_clear(tmp_path):
    path = tmp_path / "stop.request"
    flag = StopFlag(path)

    assert flag.requested() is False
    flag.request()
    assert flag.requested() is True
    assert path.read_text(encoding="utf-8") == "stop\n"
    flag.clear()
    assert flag.requested() is False
    assert not path.exists()


def test_null_flag_never_persists():
    flag = StopFlag(None)

    flag.request()
    assert flag.requested() is False
    flag.clear()
