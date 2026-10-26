"""Audit log tests."""

import json
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.audit import AuditLog, NullAudit  # noqa: E402


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
