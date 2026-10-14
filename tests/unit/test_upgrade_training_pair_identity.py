"""Pair compatibility and measured admission bind different identities independently."""

from datetime import UTC, datetime, timedelta
from importlib.metadata import PackagePath
from types import SimpleNamespace

import pytest

from coire_api.db import NodeRow
from coire_api.upgrades.qualification import training_runtimes_current
from coire_core.errors import TrainingValidationError
from coire_core.settings import Settings


@pytest.mark.parametrize("fault", [None, "missing", "stale", "duplicate", "future"])
def test_different_native_graphs_need_current_individual_observations(fault: str | None) -> None:
    now = datetime.now(UTC)
    nodes = [
        NodeRow(name="coire-edge-a", runtime_sha256="a" * 64, health_observed_at=now),
        NodeRow(name="coire-edge-b", runtime_sha256="b" * 64, health_observed_at=now),
    ]
    if fault == "missing":
        nodes[1].runtime_sha256 = None
    elif fault == "stale":
        nodes[1].health_observed_at = now - timedelta(seconds=31)
    elif fault == "duplicate":
        nodes[1].name = nodes[0].name
    elif fault == "future":
        nodes[1].health_observed_at = now + timedelta(seconds=1)
    assert training_runtimes_current(nodes, Settings(upgrades_enabled=True), now=now) is (
        fault is None
    )


def test_each_measured_node_digest_invalidates_same_version_native_content_change(monkeypatch):
    from coire_node import agent, upgrade_runtime
    from coire_scheduler import training_guard

    settings = Settings(node_name="coire-edge-a", upgrades_enabled=True)
    monkeypatch.setattr("coire_core.settings.get_settings", lambda: settings)
    native = "a" * 64
    monkeypatch.setattr(upgrade_runtime, "running_runtime_sha256", lambda _: native)
    node = NodeRow(
        name=settings.node_name,
        agent_version=agent.__version__,
        gpu_cores=80,
        memory_total_bytes=agent.psutil.virtual_memory().total,
        runtime_sha256=native,
    )
    original = training_guard.hardware_digest(node)
    assert agent.hardware_sha256(settings, 80) == original
    native = "b" * 64
    node.runtime_sha256 = native
    assert training_guard.hardware_digest(node) == agent.hardware_sha256(settings, 80)
    assert training_guard.hardware_digest(node) != original
    # The retained report's original measured identity is never rewritten.
    assert original != agent.hardware_sha256(settings, 80)


def test_shared_training_code_reads_bytes_and_ignores_only_local_bookkeeping(tmp_path, monkeypatch):
    from coire_node.training import runtime_identity

    (tmp_path / "package").mkdir()
    (tmp_path / "package/worker.py").write_text("same code")
    (tmp_path / "package/__pycache__").mkdir()
    (tmp_path / "package/__pycache__/worker.pyc").write_bytes(b"local cached bytes")
    (tmp_path / "package.dist-info").mkdir()
    (tmp_path / "package.dist-info/direct_url.json").write_text("local wheel location")
    files = [
        PackagePath("package/worker.py"),
        PackagePath("package/__pycache__/worker.pyc"),
        PackagePath("package.dist-info/direct_url.json"),
        PackagePath("../../../bin/local-launcher"),
    ]
    installed = SimpleNamespace(files=files, locate_file=lambda relative: tmp_path / relative)
    monkeypatch.setattr(runtime_identity, "distribution", lambda _: installed)
    monkeypatch.setattr(runtime_identity.sys, "prefix", str(tmp_path))
    original = runtime_identity.training_code_sha256()
    (tmp_path / "package.dist-info/direct_url.json").write_text("other Studio location")
    (tmp_path / "package/__pycache__/worker.pyc").write_bytes(b"other cache")
    assert runtime_identity.training_code_sha256() == original
    (tmp_path / "package/worker.py").write_text("changed code; same package version")
    assert runtime_identity.training_code_sha256() != original
    outside = tmp_path.parent / (tmp_path.name + "-outside.py")
    outside.write_text("escaped installed file")
    try:
        (tmp_path / "package/worker.py").unlink()
        (tmp_path / "package/worker.py").symlink_to(outside)
        with pytest.raises(TrainingValidationError):
            runtime_identity.training_code_sha256()
    finally:
        outside.unlink()


def test_missing_shared_training_distribution_inventory_cannot_qualify(monkeypatch):
    from coire_node.training import runtime_identity

    monkeypatch.setattr(runtime_identity, "distribution", lambda _: SimpleNamespace(files=None))
    with pytest.raises(TrainingValidationError):
        runtime_identity.training_code_sha256()
