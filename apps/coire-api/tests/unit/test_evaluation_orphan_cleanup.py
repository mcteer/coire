"""Stopped owned attempts remove uncommitted bytes without touching other evidence."""

import hashlib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coire_api.db import EvaluationAttemptRow, EvaluationEvidenceRow
from coire_api.evaluation.evidence import EvidenceStore
from coire_core.settings import Settings
from coire_scheduler.evaluations import cleanup_phase

WORKLOAD_FIXTURE = Path(__file__).resolve().parents[4] / "tests/fixtures/evaluations/workload.json"


@pytest.mark.parametrize("retained", [False, True])
async def test_released_attempt_cleans_only_its_uncommitted_evidence(
    tmp_path: Path, retained: bool
) -> None:
    settings = Settings(training_dataset_dir=str(tmp_path))
    store = EvidenceStore(settings)
    attempt = EvaluationAttemptRow(id=uuid.uuid4(), state="released")
    identity = uuid.uuid5(attempt.id, "evaluation-evidence-v1")
    foreign = uuid.uuid4()
    data = b"private synthetic evidence"
    digest = hashlib.sha256(data).hexdigest()
    await store.stage(identity, data, digest)
    await store.stage(foreign, data, digest)
    session = AsyncMock()
    session.get.return_value = EvaluationEvidenceRow(id=identity) if retained else None
    assert await cleanup_phase(session, attempt, settings)
    assert (store.root / str(identity)).exists() is retained
    assert (store.root / str(foreign)).exists()


async def test_crashed_staging_bytes_are_removed_only_for_the_stopped_owner(tmp_path: Path) -> None:
    settings = Settings(training_dataset_dir=str(tmp_path))
    store = EvidenceStore(settings)
    attempt = EvaluationAttemptRow(id=uuid.uuid4(), state="released")
    identity = uuid.uuid5(attempt.id, "evaluation-evidence-v1")
    owned = store.root / f"stage-{identity}-{uuid.uuid4()}"
    foreign = store.root / f"stage-{uuid.uuid4()}-{uuid.uuid4()}"
    owned.write_bytes(b"partial private evidence")
    foreign.write_bytes(b"foreign private evidence")
    session = AsyncMock()
    session.get.return_value = None
    assert await cleanup_phase(session, attempt, settings)
    assert not owned.exists()
    assert foreign.exists()


@pytest.mark.parametrize("proof", ["absent", "stopped", "stopping", "unreachable", "foreign"])
async def test_failed_owned_engine_requires_fresh_stop_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    from datetime import UTC, datetime

    from coire_api.db import EngineProcessRow, MemoryReservationRow, ModelInstanceRow, NodeRow
    from coire_api.nodes_client import NodeError, NodeErrorKind
    from coire_core.models.engine import EngineState, EngineStatus
    from coire_core.models.evaluation import EvaluationWorkload
    from coire_core.models.instance import InstanceState
    from coire_core.models.placement import MemoryReservationState

    workload = EvaluationWorkload.model_validate_json(WORKLOAD_FIXTURE.read_bytes())
    node = NodeRow(id=uuid.uuid4(), name="coire-edge-a")
    instance = ModelInstanceRow(id=uuid.uuid4(), state=InstanceState.FAILED)
    engine = EngineProcessRow(id=uuid.uuid4(), node_id=node.id, state=EngineState.FAILED)
    hold = MemoryReservationRow(id=uuid.uuid4(), state=MemoryReservationState.HELD)
    attempt = EvaluationAttemptRow(
        id=workload.attempt_id,
        state="collected",
        node_id=node.id,
        instance_id=instance.id,
        owns_instance=True,
        workload=workload.model_dump(mode="json"),
        sandbox_reservation_id=hold.id,
        resident_lease_ids=[],
    )
    session = AsyncMock()

    async def get(model: object, identity: object, **kwargs: object) -> object | None:
        if model is NodeRow:
            return node
        if model is ModelInstanceRow:
            return instance
        if model is MemoryReservationRow:
            return hold
        return None

    session.get.side_effect = get
    rows = AsyncMock()
    rows.all = lambda: [engine]
    session.scalars.return_value = rows
    client = AsyncMock()
    if proof == "unreachable":
        client.stop_engine.side_effect = NodeError(NodeErrorKind.UNREACHABLE, node.name)
    else:
        client.stop_engine.return_value = (
            None
            if proof == "absent"
            else EngineStatus(
                engine_id=engine.id,
                port=9500,
                started_at=datetime.now(UTC),
                state=EngineState.STOPPED if proof == "stopped" else EngineState.STOPPING,
            )
        )
    manager = AsyncMock()
    manager.__aenter__.return_value = client
    monkeypatch.setattr("coire_scheduler.evaluations.NodeClient", lambda _: manager)
    monkeypatch.setattr("coire_scheduler.evaluations.lock_nodes_for_admission", AsyncMock())
    confirmed = proof in {"absent", "stopped"}
    assert (
        await cleanup_phase(session, attempt, Settings(training_dataset_dir=str(tmp_path)))
        is confirmed
    )
    client.stop_engine.assert_awaited_once_with(node.name, engine.id)
    assert engine.state is (EngineState.STOPPED if confirmed else EngineState.FAILED)
    assert hold.state is (
        MemoryReservationState.RELEASED if confirmed else MemoryReservationState.HELD
    )
    assert attempt.state == ("released" if confirmed else "collected")
