"""The hot journal query preserves uncertain owners, retention holds and transactions."""

import json
import threading
from pathlib import Path

import pytest

from coire_node.training.journal import TrainingJournal


def test_active_index_tracks_release_and_rollback_without_erasing_history(tmp_path: Path) -> None:
    journal = TrainingJournal(
        tmp_path / "journal", node="coire-edge-a", admission_lock=threading.RLock()
    )
    try:
        for identity, released in (("historical", True), ("live", False), ("unknown", None)):
            body = {"attempt_id": identity, "memory_bytes": 10, "disk_bytes": 20}
            if released is not None:
                body["released"] = released
            journal.db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?)", (identity, "job", 1, json.dumps(body))
            )
        assert {row["attempt_id"] for row in journal.active_records()} == {"live", "unknown"}
        value = journal.get("live")
        value["released"] = True
        with pytest.raises(RuntimeError), journal.transaction():
            journal.save(value)
            assert {row["attempt_id"] for row in journal.active_records()} == {"unknown"}
            raise RuntimeError("rollback")
        assert {row["attempt_id"] for row in journal.active_records()} == {"live", "unknown"}
        with journal.transaction():
            journal.save(value)
        assert len(journal.records()) == 3
        assert {row["attempt_id"] for row in journal.active_records()} == {"unknown"}
        # Disk holds remain visible through the unfiltered durable history.
        assert sum(row["disk_bytes"] for row in journal.records()) == 60
        plan = journal.db.execute(
            "EXPLAIN QUERY PLAN SELECT body FROM attempts WHERE json_extract(body, '$.released') IS NOT 1"
        ).fetchall()
        assert any("attempts_unreleased" in row[3] for row in plan)
    finally:
        journal.close()
    reopened = TrainingJournal(
        tmp_path / "journal", node="coire-edge-a", admission_lock=threading.RLock()
    )
    try:
        assert {row["attempt_id"] for row in reopened.active_records()} == {"unknown"}
        assert len(reopened.records()) == 3
    finally:
        reopened.close()
