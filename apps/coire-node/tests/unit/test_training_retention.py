"""Immutable cleanup intents survive interruption without deleting live artifacts."""

import threading
import uuid
from pathlib import Path

import pytest
from training_artifact_fixtures import artifact, grant
from training_measurement_fixtures import experiment

from coire_core.errors import TrainingConflict
from coire_core.models.training_node import (
    TrainingArtifactDeleteRequest,
    TrainingAttemptCleanupRequest,
)
from coire_node.training.artifacts import TrainingArtifacts
from coire_node.training.journal import TrainingJournal
from coire_node.training.retention import cleanup_attempt
from coire_node.training.supervisor import TrainingSupervisor


def test_deletion_requires_local_reference_proof_and_exact_manifest(tmp_path: Path) -> None:
    manifest = artifact(tmp_path)
    command = TrainingArtifactDeleteRequest(
        command_id=uuid.uuid4(),
        manifest_sha256=manifest.canonical_sha256(),
        expected_version=1,
        unreferenced=True,
    )
    manager = TrainingArtifacts(tmp_path, node_name="coire-edge-a")
    with pytest.raises(ValueError, match="reference"):
        manager.delete(manifest.artifact_id, command)
    manager.referenced = lambda _: True
    with pytest.raises(ValueError, match="reference"):
        manager.delete(manifest.artifact_id, command)
    manager.referenced = lambda _: False
    with pytest.raises(ValueError, match="manifest"):
        manager.delete(
            manifest.artifact_id, command.model_copy(update={"manifest_sha256": "f" * 64})
        )
    assert manager.manifest(manifest.artifact_id) == manifest
    issued = manager.issue(grant(manifest))
    with pytest.raises(ValueError, match="grant"):
        manager.delete(manifest.artifact_id, command)
    manager.revoke(issued.grant_id)
    receipt = manager.delete(manifest.artifact_id, command)
    assert receipt.purged and not (tmp_path / str(manifest.artifact_id)).exists()
    restarted = TrainingArtifacts(tmp_path, node_name="coire-edge-a", referenced=lambda _: False)
    assert restarted.delete(manifest.artifact_id, command) == receipt
    with pytest.raises(ValueError, match="intent"):
        restarted.delete(
            manifest.artifact_id, command.model_copy(update={"command_id": uuid.uuid4()})
        )


def test_failed_purge_is_hidden_durable_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    manifest = artifact(tmp_path)
    command = TrainingArtifactDeleteRequest(
        command_id=uuid.uuid4(),
        manifest_sha256=manifest.canonical_sha256(),
        expected_version=1,
        unreferenced=True,
    )
    manager = TrainingArtifacts(tmp_path, node_name="coire-edge-a", referenced=lambda _: False)
    original = shutil.rmtree

    def interrupted(path: Path) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("coire_node.training.artifacts.shutil.rmtree", interrupted)
    with pytest.raises(OSError):
        manager.delete(manifest.artifact_id, command)
    with pytest.raises(ValueError):
        manager.manifest(manifest.artifact_id)
    assert (tmp_path / f".deleting-{manifest.artifact_id}").is_dir()
    monkeypatch.setattr("coire_node.training.artifacts.shutil.rmtree", original)
    restarted = TrainingArtifacts(tmp_path, node_name="coire-edge-a", referenced=lambda _: False)
    assert restarted.delete(manifest.artifact_id, command).purged
    assert not (tmp_path / f".deleting-{manifest.artifact_id}").exists()


def test_manifest_sized_deletion_intent_replays_after_restart(tmp_path: Path) -> None:
    from coire_core.models.training_node import TrainingArtifactManifest

    manifest = artifact(tmp_path)
    directory = tmp_path / str(manifest.artifact_id)
    files = []
    for index in range(32):
        entry = manifest.files[0].model_copy(
            update={"id": f"tensor-{index}", "name": f"tensor-{index}.safetensors"}
        )
        (directory / entry.name).write_bytes(b"synthetic-artifact-bytes")
        files.append(entry)
    (directory / manifest.files[0].name).unlink()
    manifest = TrainingArtifactManifest.model_validate(
        manifest.model_copy(
            update={"files": files, "total_bytes": sum(entry.bytes for entry in files)}
        ).model_dump(mode="json")
    )
    (directory / "manifest.json").write_text(manifest.model_dump_json())
    request = TrainingArtifactDeleteRequest(
        command_id=uuid.uuid4(),
        manifest_sha256=manifest.canonical_sha256(),
        expected_version=1,
        unreferenced=True,
        expected_manifest=manifest,
    )
    manager = TrainingArtifacts(tmp_path, node_name="coire-edge-a", referenced=lambda _: False)
    receipt = manager.delete(manifest.artifact_id, request)
    assert (tmp_path / f".deletion-{manifest.artifact_id}.json").stat().st_size > 4096
    restarted = TrainingArtifacts(tmp_path, node_name="coire-edge-a", referenced=lambda _: False)
    assert restarted.delete(manifest.artifact_id, request) == receipt


def test_missing_linked_and_unlisted_content_cannot_be_claimed_purged(tmp_path: Path) -> None:
    manifest = artifact(tmp_path)
    manager = TrainingArtifacts(tmp_path, node_name="coire-edge-a", referenced=lambda _: False)
    command = TrainingArtifactDeleteRequest(
        command_id=uuid.uuid4(),
        manifest_sha256=manifest.canonical_sha256(),
        expected_version=1,
        unreferenced=True,
    )
    with pytest.raises(ValueError):
        manager.delete(uuid.uuid4(), command)
    extra = tmp_path / str(manifest.artifact_id) / "unlisted"
    extra.write_bytes(b"unowned")
    with pytest.raises(ValueError, match="unlisted"):
        manager.delete(manifest.artifact_id, command)
    extra.unlink()
    extra.symlink_to(tmp_path.parent)
    with pytest.raises(ValueError):
        manager.delete(manifest.artifact_id, command)
    assert extra.is_symlink()


def test_live_reference_tracking_protects_resume_and_current_points_not_old_history(
    tmp_path: Path,
) -> None:
    _, dispatch = experiment()
    command = dispatch.commands[0].prepare
    resume, staged, committed, old = [uuid.uuid4() for _ in range(4)]
    command.resume_checkpoint_id, command.resume_manifest_sha256 = resume, "a" * 64
    journal = TrainingJournal(
        tmp_path / "journal", node=command.node, admission_lock=threading.RLock()
    )
    supervisor = TrainingSupervisor(journal, interpreter=Path("/opt/coire/envs/test/bin/python3"))
    try:
        journal.prepare(command, memory_available=10**12, disk_available=10**12, disk_floor=0)
        with journal.transaction():
            record = journal.get(command.attempt_id)
            record["latest_staged_artifact_id"] = str(staged)
            record["latest_checkpoint_id"] = str(committed)
            journal.save(record)
        assert all(
            supervisor.references_artifact(identity) for identity in (resume, staged, committed)
        )
        assert not supervisor.references_artifact(old)
        with journal.transaction():
            record = journal.get(command.attempt_id)
            record.pop("artifact_tracking_version")
            journal.save(record)
        assert supervisor.references_artifact(old)  # Legacy uncertainty retains protection.
        with journal.transaction():
            record = journal.get(command.attempt_id)
            record["liveness"] = "stopped"
            journal.save(record)
        journal.release_after_death(command.attempt_id)
        assert not supervisor.references_artifact(old)
    finally:
        journal.close()


def test_retired_attempt_workspace_purge_replays_without_discarding_serving_artifact(
    tmp_path: Path,
) -> None:
    _, dispatch = experiment()
    prepared = dispatch.commands[0].prepare
    journal = TrainingJournal(
        tmp_path / "journal", node=prepared.node, admission_lock=threading.RLock()
    )
    supervisor = TrainingSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/test/bin/python3"),
        artifact_root=tmp_path / "artifacts",
    )
    request = TrainingAttemptCleanupRequest(
        command_id=uuid.uuid4(),
        job_id=prepared.job_id,
        attempt_id=prepared.attempt_id,
        fence=prepared.fence,
        node=prepared.node,
        prepared_command_id=prepared.command_id,
    )
    try:
        journal.prepare(prepared, memory_available=10**12, disk_available=10**12, disk_floor=0)
        workspace = supervisor.directory(prepared.attempt_id)
        (workspace / "private-source.jsonl").write_bytes(b"synthetic private input")
        serving = artifact(supervisor.artifact_root)
        with pytest.raises(TrainingConflict, match="death"):
            cleanup_attempt(supervisor, request)
        with journal.transaction():
            record = journal.get(prepared.attempt_id)
            record["liveness"] = "stopped"
            journal.save(record)
        journal.release_after_death(prepared.attempt_id)
        receipt = cleanup_attempt(supervisor, request)
        assert receipt.purged and not workspace.exists()
        assert journal.held_bytes() == (0, 0)
        assert (supervisor.artifact_root / str(serving.artifact_id)).is_dir()
        assert cleanup_attempt(supervisor, request) == receipt
        with pytest.raises(TrainingConflict, match="intent"):
            cleanup_attempt(supervisor, request.model_copy(update={"command_id": uuid.uuid4()}))
    finally:
        journal.close()


def test_failed_attempt_purge_retains_disk_hold_and_hidden_bytes_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, dispatch = experiment()
    prepared = dispatch.commands[0].prepare
    journal = TrainingJournal(
        tmp_path / "journal", node=prepared.node, admission_lock=threading.RLock()
    )
    supervisor = TrainingSupervisor(journal, interpreter=Path("/opt/coire/envs/test/bin/python3"))
    request = TrainingAttemptCleanupRequest(
        command_id=uuid.uuid4(),
        job_id=prepared.job_id,
        attempt_id=prepared.attempt_id,
        fence=prepared.fence,
        node=prepared.node,
        prepared_command_id=prepared.command_id,
    )
    try:
        journal.prepare(prepared, memory_available=10**12, disk_available=10**12, disk_floor=0)
        workspace = supervisor.directory(prepared.attempt_id)
        (workspace / "input").write_bytes(b"retained until confirmed purge")
        with journal.transaction():
            record = journal.get(prepared.attempt_id)
            record["liveness"] = "stopped"
            journal.save(record)
        journal.release_after_death(prepared.attempt_id)
        before = journal.held_bytes()[1]
        import shutil

        original = shutil.rmtree

        def unavailable(path: Path) -> None:
            raise OSError("synthetic failure")

        monkeypatch.setattr("coire_node.training.retention.shutil.rmtree", unavailable)
        with pytest.raises(OSError):
            cleanup_attempt(supervisor, request)
        assert journal.held_bytes()[1] == before
        assert (journal.root / f".cleanup-{prepared.attempt_id}").is_dir()
        monkeypatch.setattr("coire_node.training.retention.shutil.rmtree", original)
        assert cleanup_attempt(supervisor, request).purged
        assert journal.held_bytes()[1] == 0
    finally:
        journal.close()


def test_authoritative_cleanup_can_erase_corruption_or_partial_import_without_false_readiness(
    tmp_path: Path,
) -> None:
    import shutil

    manifest = artifact(tmp_path)
    manager = TrainingArtifacts(tmp_path, node_name="coire-edge-a", referenced=lambda _: False)
    command = TrainingArtifactDeleteRequest(
        command_id=uuid.uuid4(),
        manifest_sha256=manifest.canonical_sha256(),
        expected_version=1,
        unreferenced=True,
        expected_manifest=manifest,
    )
    (tmp_path / str(manifest.artifact_id) / "adapter.safetensors").write_bytes(b"corrupt")
    assert manager.delete(manifest.artifact_id, command).purged
    missing = artifact(tmp_path)
    shutil.rmtree(tmp_path / str(missing.artifact_id))
    staging = tmp_path / f".import-{missing.artifact_id}"
    staging.mkdir(mode=0o700)
    (staging / "adapter.safetensors").write_bytes(b"partial")
    request = TrainingArtifactDeleteRequest(
        command_id=uuid.uuid4(),
        manifest_sha256=missing.canonical_sha256(),
        expected_version=1,
        unreferenced=True,
        expected_manifest=missing,
    )
    assert manager.delete(missing.artifact_id, request).purged
    assert not staging.exists()
