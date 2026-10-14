"""Real private bytes, read-only preservation fingerprints and unsafe-tree refusal."""

import hashlib
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings
from coire_node.expired_recovery_native_data import capture_native_data, fingerprint_store


def test_fixed_native_data_fingerprints_stream_without_mutating_files(tmp_path):
    plan, _, _ = native_plan(tmp_path / "graphs")
    state = tmp_path / "state"
    datasets = tmp_path / "datasets"
    datasets.mkdir(mode=0o700)
    artifacts = state / "training/artifacts"
    artifacts.mkdir(mode=0o700, parents=True)
    data = datasets / "private.jsonl"
    adapter = artifacts / "private.safetensors"
    data.write_bytes(b"private dataset content")
    adapter.write_bytes(b"private adapter content")
    for file in (data, adapter):
        file.chmod(0o600)
    settings = Settings(
        node_name="coire-edge-a", node_state_dir=str(state), training_dataset_dir=str(datasets)
    )
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (data, adapter)}
    first = capture_native_data(
        settings, plan, deadline_at=datetime.now(UTC) + timedelta(seconds=5)
    )
    second = capture_native_data(
        settings, plan, deadline_at=datetime.now(UTC) + timedelta(seconds=5)
    )
    assert first.root_sha256s == second.root_sha256s
    assert first.file_counts == {"datasets": 1, "artifacts": 1, "components": 0}
    assert first.total_bytes == len(data.read_bytes()) + len(adapter.read_bytes())
    assert first.root_sha256s["components"] == hashlib.sha256(b"").hexdigest()
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (data, adapter)} == before
    wire = first.model_dump_json()
    assert "private.jsonl" not in wire and "private dataset content" not in wire
    adapter.write_bytes(b"changed adapter content")
    changed = capture_native_data(
        settings, plan, deadline_at=datetime.now(UTC) + timedelta(seconds=5)
    )
    assert first.root_sha256s["artifacts"] != changed.root_sha256s["artifacts"]


@pytest.mark.parametrize("fault", ["root_link", "file_link", "hard_link", "writable", "elapsed"])
def test_native_data_refuses_linked_mutable_or_out_of_budget_scope(tmp_path, fault):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    data = root / "retained"
    data.write_bytes(b"retained data")
    data.chmod(0o600)
    if fault == "root_link":
        linked = tmp_path / "linked"
        linked.symlink_to(root)
        root = linked
    elif fault == "file_link":
        (root / "linked").symlink_to(data)
    elif fault == "hard_link":
        os.link(data, root / "second")
    elif fault == "writable":
        data.chmod(0o666)
    with pytest.raises(UpgradeUnavailable):
        fingerprint_store(root, time.monotonic() + (-1 if fault == "elapsed" else 5))
