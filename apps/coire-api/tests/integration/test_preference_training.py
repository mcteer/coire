"""Real Postgres v3 preflight/admission; synthetic evidence is not Studio qualification."""

import os
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_training_measurement_transactions import measurement_db as measurement_db
from training_measurement_fixtures import observation

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    MemoryReservationRow,
    NodeMemoryLedgerRow,
    NodeRow,
    TrainingCommandRow,
    TrainingMeasurementRow,
)
from coire_api.nodes_client import NodeClient
from coire_api.training.measurements import (
    freeze_measurement_inputs,
    mint_measurement_inputs,
    submit_measurement,
)
from coire_api.training.service import payload_digest
from coire_core.errors import TrainingUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.preference import PreferenceSplitManifest
from coire_core.models.training import (
    PreferenceMemoryEvidence,
    ResolvedTrainingSpecV3,
    TrainingMeasurementRequest,
)
from coire_core.models.training_node import (
    PreferenceMeasurementBinding,
    PreferenceMeasurementObservation,
)
from coire_core.settings import Settings
from coire_scheduler.training_guard import hardware_digest
from coire_scheduler.training_measurements import admit_measurement, build_report

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires disposable Postgres"
    ),
]


@pytest.mark.parametrize("measurement_db", ["preference-dpo", "preference-orpo"], indirect=True)
async def test_preference_measurement_refuses_impossible_fit_without_holds(
    measurement_db: async_sessionmaker[AsyncSession],
) -> None:
    async with measurement_db.begin() as session:
        row = await session.scalar(select(TrainingMeasurementRow))
        assert row is not None
        request = TrainingMeasurementRequest.model_validate(row.request)
        binding = await freeze_measurement_inputs(session, request)
        ledger = await session.scalar(select(NodeMemoryLedgerRow))
        assert ledger is not None
        before = {
            hold.id: hold.state for hold in await session.scalars(select(MemoryReservationRow))
        }
        ledger.budget_bytes = 1
        await session.flush()
        assert await admit_measurement(session, row, binding) is None
        assert {
            hold.id: hold.state for hold in await session.scalars(select(MemoryReservationRow))
        } == before
        assert row.state == "queued"


@pytest.mark.parametrize("measurement_db", ["preference-dpo", "preference-orpo"], indirect=True)
async def test_preference_measurement_gate_capability_freeze_admission_and_report(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(training_enabled=True, preference_training_enabled=True)
    async with measurement_db.begin() as session:
        initial = await session.scalar(select(TrainingMeasurementRow))
        assert initial is not None
        request = TrainingMeasurementRequest.model_validate(initial.request)
        principal = Principal(
            kind=PrincipalKind.USER, user_id=initial.owner_user_id, role=UserRole.ADMIN
        )
        nodes = {node.name: node for node in await session.scalars(select(NodeRow))}
        advertised = [1, 2]

        async def capability(
            _client: NodeClient, method: str, node: str, path: str, **kwargs: Any
        ) -> tuple[int, object]:
            assert method == "GET" and path == "/node/training/measurements/capabilities"
            return 200, {
                "node": node,
                "hardware_sha256": hardware_digest(nodes[node]),
                "spec_versions": advertised,
                "world_sizes": [1],
                "measurement_checkpoint": True,
            }

        monkeypatch.setattr(NodeClient, "_call", capability)
        with pytest.raises(TrainingUnavailable, match="disabled"):
            await submit_measurement(
                session, principal, request, "v3-disabled", settings=Settings(training_enabled=True)
            )
        with pytest.raises(TrainingUnavailable, match="version"):
            await submit_measurement(session, principal, request, "v3-old-node", settings=settings)
        advertised.append(3)
        receipt = await submit_measurement(
            session, principal, request, "v3-measure", settings=settings
        )
        replay = await submit_measurement(
            session, principal, request, "v3-measure", settings=Settings(training_enabled=True)
        )
        assert replay == receipt
        binding = await freeze_measurement_inputs(session, request)
        assert isinstance(binding, PreferenceMeasurementBinding)
        assert all(isinstance(source.split, PreferenceSplitManifest) for source in binding.sources)
        row = await session.get(TrainingMeasurementRow, receipt.measurement_id)
        assert row is not None
        dispatch = await admit_measurement(session, row, binding)
        assert dispatch is not None
        probe = dispatch.commands[0]
        resolved = probe.prepare.resolved
        assert isinstance(resolved, ResolvedTrainingSpecV3)
        assert resolved.initial_target == binding.initial_target
        assert resolved.reference_target == binding.reference_target
        assert (resolved.resource_envelope.reference_weight_bytes > 0) == (
            resolved.spec.objective == "dpo"
        )
        hold = await session.get(MemoryReservationRow, probe.prepare.reservation_id)
        assert (
            hold is not None
            and hold.bytes == resolved.resource_envelope.memory_bytes == 7 * 1024**3
        )
        command = await session.scalar(
            select(TrainingCommandRow).where(
                TrainingCommandRow.operation == "training.measurement",
                TrainingCommandRow.subject_id == str(row.id),
            )
        )
        assert command is not None
        command.payload = {**command.payload, "dispatch": dispatch.model_dump(mode="json")}
        delivery = await mint_measurement_inputs(session, principal, probe, settings=settings)
        assert len(delivery.sources) == len(binding.sources)
        assert all(isinstance(source.split, PreferenceSplitManifest) for source in delivery.sources)
        native = PreferenceMeasurementObservation.model_validate(
            {
                **observation(probe).model_dump(),
                "schema_version": 3,
                "objective": resolved.spec.objective,
                "initial_target": resolved.initial_target,
                "reference_target": resolved.reference_target,
                "reference_weight_bytes": 32 * 1024**2 if resolved.spec.objective == "dpo" else 0,
                "reference_adapter_bytes": 0,
                "probe_count": request.spec.optim.updates,
            }
        )
        report = build_report(row, dispatch, [native], [], settings)
        assert isinstance(report.memory_evidence, PreferenceMemoryEvidence)
        assert report.memory_evidence.initial_target == binding.initial_target
        assert report.memory_evidence.datasets == resolved.datasets
        assert report.node_report_sha256 == [payload_digest(native)]
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from coire_api.db import TrainingProfileRow
        from coire_api.training.specs import resolve_submission
        from coire_core.models.training import TrainingSubmission
        from coire_core.models.training_node import TrainingStopReceipt
        from coire_scheduler.training_measurements import (
            MeasurementNodeClient,
            TrainingMeasurementExecutor,
        )

        executor = TrainingMeasurementExecutor(settings, MeasurementNodeClient(settings))
        await executor.finish(session, row.id, principal, binding, dispatch, report, [None])
        assert row.state == "running" and hold.state.value == "held"
        assert await session.scalar(select(TrainingProfileRow.id)) is None
        proof = TrainingStopReceipt(
            attempt_id=probe.prepare.attempt_id,
            fence=probe.prepare.fence,
            node=probe.prepare.node,
            stopped=True,
            observed_at=datetime.now(UTC),
            pid=123,
            process_create_time=1.0,
        )
        await executor.finish(session, row.id, principal, binding, dispatch, report, [proof])
        assert row.state == "succeeded" and hold.state.value == "released"
        assert await session.scalar(select(TrainingProfileRow.id)) is not None

        async def health(_client: NodeClient, node: str) -> Any:
            return SimpleNamespace(training_capabilities=SimpleNamespace(spec_versions=[1, 2, 3]))

        monkeypatch.setattr(NodeClient, "health", health)
        validation = await resolve_submission(
            session,
            TrainingSubmission(source_yaml=request.spec.model_dump_json()),
            settings=settings,
            principal=principal,
        )
        assert validation.ready_to_run and isinstance(
            validation.resolved, ResolvedTrainingSpecV3
        ), validation
        assert validation.resolved.initial_target == binding.initial_target
        assert validation.resolved.resource_envelope == report.memory_evidence.resource_envelope
