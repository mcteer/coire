from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest

from coire_core.models import ShardGroupCommand, ShardGroupState, ShardingMode, ShardRank
from coire_core.settings import Settings
from coire_node.sharding import ShardGroupManager, build_shard_argv
from coire_node.store import Store


def command() -> ShardGroupCommand:
    return ShardGroupCommand(
        command_id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        variant_id=uuid.uuid4(),
        slug="coire--qwen-4bit",
        mode=ShardingMode.TENSOR_PARALLEL,
        ranks=[
            ShardRank(rank=0, node_name="coire-edge-a", host="coire-edge-a.fabric", port=9600),
            ShardRank(rank=1, node_name="coire-edge-b", host="coire-edge-b.fabric", port=9600),
        ],
        estimate_bytes_per_rank=1024,
        hostfile_sha256="0" * 64,
    )


def test_shard_argv_is_bare_fixed_and_uses_data_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COIRE_SHARD_COMMAND", raising=False)
    argv = build_shard_argv(
        command(),
        model_path="/opt/coire/models/coire--qwen-4bit",
        hostfile_path="/state/hosts.json",
    )
    assert argv[1:3] == ["-m", "mlx.launch"]
    assert "jaccl" in argv and "mlx_lm.server" in argv
    assert "-n" not in argv
    assert "MLX_METAL_FAST_SYNCH=1" in argv
    assert "/opt/coire/models/coire--qwen-4bit" in argv
    assert not any("coire-core" in value for value in argv)


def test_rank_one_prepare_is_persisted_and_idempotent(tmp_path: Path) -> None:
    settings = Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        node_name="coire-edge-b",
        node_store_dir=str(tmp_path / "models"),
        node_state_dir=str(tmp_path / "state"),
        sharding_jaccl_hostfile=str(tmp_path / "state" / "jaccl.json"),
    )
    store = Store(settings.node_store_dir)
    store.ensure_root()
    store.path_for("coire--qwen-4bit").mkdir()
    cmd = command()
    encoded = json.dumps({"backend": "jaccl", "hosts": []}).encode()
    hostfile = tmp_path / "state" / "jaccl.json"
    hostfile.parent.mkdir(parents=True)
    hostfile.write_bytes(encoded)
    cmd.hostfile_sha256 = hashlib.sha256(encoded).hexdigest()
    manager = ShardGroupManager(settings, store)
    first = manager.prepare(cmd)
    second = manager.prepare(cmd)
    assert first.group_id == second.group_id
    assert (tmp_path / "state" / "shard-groups.json").is_file()
    adopted = ShardGroupManager(settings, store).get(cmd.group_id)
    assert adopted is not None
    assert adopted.state is ShardGroupState.FAILED
    assert "restarted" in (adopted.state_reason or "")


def test_partial_launch_failure_is_persisted_and_group_can_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        node_name="coire-edge-a",
        node_store_dir=str(tmp_path / "models"),
        node_state_dir=str(tmp_path / "state"),
        sharding_jaccl_hostfile=str(tmp_path / "state" / "jaccl.json"),
    )
    store = Store(settings.node_store_dir)
    store.ensure_root()
    store.path_for("coire--qwen-4bit").mkdir()
    encoded = json.dumps({"backend": "jaccl", "hosts": []}).encode()
    hostfile = tmp_path / "state" / "jaccl.json"
    hostfile.parent.mkdir(parents=True)
    hostfile.write_bytes(encoded)
    cmd = command()
    cmd.hostfile_sha256 = hashlib.sha256(encoded).hexdigest()
    monkeypatch.setenv("COIRE_SHARD_COMMAND", "/usr/bin/false")
    manager = ShardGroupManager(settings, store)
    manager.prepare(cmd)
    deadline = time.monotonic() + 2
    failed = manager.get(cmd.group_id)
    while time.monotonic() < deadline:
        failed = manager.get(cmd.group_id)
        if failed is not None and failed.state is ShardGroupState.FAILED:
            break
        time.sleep(0.01)
    assert failed is not None
    assert failed.state is ShardGroupState.FAILED
    stopped = manager.stop(cmd.group_id)
    assert stopped is not None
    assert stopped.state is ShardGroupState.STOPPED


def test_capability_names_architecture_and_uses_model_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        node_name="coire-edge-b",
        node_store_dir=str(tmp_path / "models"),
        node_state_dir=str(tmp_path / "state"),
    )
    store = Store(settings.node_store_dir)
    store.ensure_root()
    model_path = store.path_for("coire--qwen-4bit")
    model_path.mkdir()
    (model_path / "config.json").write_text(json.dumps({"architectures": ["Qwen3ForCausalLM"]}))
    monkeypatch.setenv("COIRE_TEST_FAKE_VALIDATION", "1")
    result = ShardGroupManager(settings, store).capability(
        "coire--qwen-4bit", ShardingMode.TENSOR_PARALLEL
    )
    assert result.supported is True
    assert result.architecture == "Qwen3ForCausalLM"
