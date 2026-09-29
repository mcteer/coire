"""Coding activity receipts contain bounded lifecycle metadata only."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from coire_agent.activity import ActivitySpool
from coire_core.models.runs import RunActivity


def _records(path: Path) -> list[RunActivity]:
    return [RunActivity.model_validate_json(line) for line in path.read_bytes().splitlines()]


def test_activity_records_lifecycle_without_arguments_or_error_text(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    path = tmp_path / "activity.jsonl"
    spool = ActivitySpool(path, run_id)
    with spool.step("read_file"):
        pass
    with pytest.raises(RuntimeError, match="private source content"), spool.step("run_tests"):
        raise RuntimeError("private source content")
    records = _records(path)
    assert [record.sequence for record in records] == [1, 2, 3, 4]
    assert [record.state for record in records] == [
        "started",
        "completed",
        "started",
        "failed",
    ]
    assert {record.run_id for record in records} == {run_id}
    assert records[-1].safe_error == "operation_failed"
    assert records[0].tool_call_id == records[1].tool_call_id
    assert records[2].tool_call_id == records[3].tool_call_id
    assert records[0].tool_call_id != records[2].tool_call_id
    assert b"private source content" not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600


def test_activity_spool_emits_one_overflow_marker_and_stops(tmp_path: Path) -> None:
    path = tmp_path / "activity.jsonl"
    spool = ActivitySpool(path, uuid.uuid4(), max_records=4)
    for _ in range(8):
        spool.record("read_file", "completed")
    records = _records(path)
    assert [record.sequence for record in records] == [1, 2, 3, 4]
    assert records[-1].tool_name == "activity_spool"
    assert records[-1].safe_error == "limit_reached"
    assert spool.truncated


def test_activity_spool_caps_bytes_and_rejects_foreign_tool_or_symlink(tmp_path: Path) -> None:
    path = tmp_path / "activity.jsonl"
    spool = ActivitySpool(path, uuid.uuid4(), max_bytes=2048)
    with pytest.raises(ValueError, match="not allowed"):
        spool.record("/workspace/secret.txt", "started")
    with pytest.raises(ValueError, match="not allowed"):
        spool.record("read_file", "failed", safe_error="private exception")
    for _ in range(30):
        spool.record("read_file", "completed")
    records = _records(path)
    assert records[-1].tool_name == "activity_spool"
    assert path.stat().st_size <= 2048
    assert all(
        set(json.loads(line)) <= set(RunActivity.model_fields)
        for line in path.read_text().splitlines()
    )

    target = tmp_path / "target"
    target.write_text("untouched")
    linked = tmp_path / "linked.jsonl"
    linked.symlink_to(target)
    with pytest.raises(OSError):
        ActivitySpool(linked, uuid.uuid4()).record("read_file", "started")
    assert target.read_text() == "untouched"
