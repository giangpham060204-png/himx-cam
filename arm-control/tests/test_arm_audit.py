"""Audit log tests."""

import json
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from arm_control.audit import AuditLog, NullAudit  # noqa: E402


def test_audit_writes_one_json_line_per_event(tmp_path):
    path = tmp_path / "nested" / "arm.jsonl"
    log = AuditLog(path)
    log.record("move", axis="m1", position=10.0, config_hash="abc123")
    log.record("refuse", axis="m2", error="out of range")
    log.close()

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "move"
    assert first["axis"] == "m1"
    assert first["position"] == 10.0
    assert first["config_hash"] == "abc123"
    datetime.fromisoformat(first["timestamp"])
    assert json.loads(lines[1])["event"] == "refuse"


def test_null_audit_accepts_anything():
    NullAudit().record("anything", axis="m1")


def test_audit_context_is_written(tmp_path):
    log = AuditLog(tmp_path / "arm.jsonl", context={"commit": "abc123"})
    log.record("move", axis="m1")
    log.close()

    entry = json.loads((tmp_path / "arm.jsonl").read_text().splitlines()[0])
    assert entry["commit"] == "abc123"
    assert entry["axis"] == "m1"
