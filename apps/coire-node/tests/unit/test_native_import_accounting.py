"""Import retries reserve fresh scopes; cancellation cannot abandon an owned copy."""

import asyncio
import threading
import uuid
from pathlib import Path

import pytest

from coire_core.models.training_node import (
    TrainingArtifactFile,
    TrainingArtifactImportRequest,
    TrainingArtifactManifest,
)
from coire_node.agent import AccountedArtifactImporter
from coire_node.reservations import ReservationLedger
from coire_node.testing.harness import Agent
from coire_node.training.artifacts import TrainingArtifacts
from coire_node.training.importer import ArtifactImporter
from coire_node.training.journal import TrainingJournal
from coire_node.training.supervisor import TrainingSupervisor


@pytest.mark.asyncio
async def test_import_retry_and_cancel_keep_real_protected_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(tmp_path / "node", node_name="coire-edge-a")
    lock = threading.RLock()
    journal = TrainingJournal(tmp_path / "attempts", node="coire-edge-a", admission_lock=lock)
    training = TrainingSupervisor(journal, interpreter=Path("/opt/coire/envs/v1/bin/python"))
    ledger = ReservationLedger(agent.settings, agent.store, lambda: 0, memory_lock=lock)
    artifacts = TrainingArtifacts(tmp_path / "artifacts", node_name="coire-edge-a")
    importer = AccountedArtifactImporter(
        artifacts,
        tmp_path / "imports",
        port=9401,
        reservations=ledger,
        training=training,
        disk_floor_bytes=0,
    )
    artifact = uuid.uuid4()
    manifest = TrainingArtifactManifest(
        artifact_id=artifact,
        kind="adapter",
        total_bytes=3,
        files=[
            TrainingArtifactFile(id="adapter", name="adapter.safetensors", bytes=3, sha256="a" * 64)
        ],
    )
    request = TrainingArtifactImportRequest(
        command_id=uuid.uuid4(),
        artifact_id=artifact,
        manifest_sha256=manifest.canonical_sha256(),
        source_node="coire-edge-b",
        destination_node="coire-edge-a",
        attempt_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        fence=1,
        grant_id=uuid.uuid4(),
        grant_secret="x" * 32,
    )

    async def fetch(self, body):  # type: ignore[no-untyped-def]
        return manifest

    monkeypatch.setattr(ArtifactImporter, "_fetch_manifest", fetch)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def copy(self, body):  # type: ignore[no-untyped-def]
        await self._fetch_manifest(body)
        entered.set()
        await finish.wait()

    monkeypatch.setattr(ArtifactImporter, "_run", copy)
    first: set[uuid.UUID] = set()
    try:
        for retry in range(2):
            task = asyncio.create_task(importer._run(request))
            await entered.wait()
            scopes = {identity for identity, _ in ledger.owner_scopes("artifact-import")}
            assert len(scopes) == 2 and scopes.isdisjoint(first)
            assert ledger.held_bytes() == 192 * 1024**2 + 1
            assert not any(ledger.release(identity) for identity in scopes)
            if retry:
                task.cancel()
                await asyncio.sleep(0)  # Deliver cancellation; the copy remains owned.
                assert ledger.held_bytes() == 192 * 1024**2 + 1 and not task.done()
            finish.set()
            if retry:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
            assert ledger.held_bytes() == 0
            first = scopes
            entered.clear()
            finish.clear()
    finally:
        finish.set()
        await importer.aclose()
        journal.close()
        agent.close()
