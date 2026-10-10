"""Real Postgres admission races and authenticated probe grant lifetime."""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from training_measurement_fixtures import (
    DIGEST,
    experiment,
    frozen_binding,
    mixture_experiment,
    observation,
)

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    Base,
    MemoryReservationRow,
    ModelRow,
    ModelVariantRow,
    NodeMemoryLedgerRow,
    NodeRow,
    TrainingCommandRow,
    TrainingDatasetAnalysisRow,
    TrainingDatasetRevisionRow,
    TrainingMeasurementRow,
    TrainingProfileRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.training.measurements import (
    authorized_measurement_source,
    freeze_measurement_inputs,
    mint_measurement_inputs,
    submit_measurement,
)
from coire_api.training.service import payload_digest
from coire_core.errors import (
    TrainingConflict,
    TrainingNotFound,
    TrainingUnavailable,
    TrainingValidationError,
)
from coire_core.models.auth import UserRole
from coire_core.models.placement import MemoryReservationState
from coire_core.models.training import TrainingMeasurementRequest
from coire_core.models.training_node import (
    TrainingMeasurementCapabilities,
    TrainingMeasurementDispatch,
    TrainingStopReceipt,
)
from coire_core.settings import Settings
from coire_scheduler.training_measurements import (
    MeasurementNodeClient,
    TrainingMeasurementExecutor,
    admit_measurement,
    build_report,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1", reason="requires disposable Postgres 17"
    ),
]


@pytest.fixture
async def measurement_db(
    training_postgres_url: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if getattr(request, "param", "single") in {"preference-dpo", "preference-orpo"}:
        from preference_measurement_fixtures import preference_experiment

        row, dispatch, _ = preference_experiment(
            "dpo" if request.param == "preference-dpo" else "orpo"
        )
    elif getattr(request, "param", "single") == "mixture":
        row, dispatch, _ = mixture_experiment(tmp_path)
    else:
        row, dispatch = experiment()
    source = dispatch.sources[0]
    async with factory.begin() as session:
        session.add(
            UserRow(
                id=row.owner_user_id,
                email="measurement@training.test",
                display_name="admin",
                role=UserRole.ADMIN,
                active=True,
            )
        )
        session.add(
            ModelRow(
                id=source.binding.model_id,
                repo_id="synthetic/measurement",
                slug="fixture",
                display_name="fixture",
                state="ready",
                visibility="admin_only",
                placement_policy="single:auto",
                memory_estimate_bytes=100,
                idle_ttl_seconds=900,
                precision="bf16",
                weight_bytes=100,
                total_bytes=100,
                file_count=1,
            )
        )
        await session.flush()
        session.add(
            ModelVariantRow(
                id=source.binding.variant_id,
                model_id=source.binding.model_id,
                name="fixture",
                slug="fixture",
                precision="bf16",
                state="ready",
                source_revision="fixture",
            )
        )
        await session.flush()
        for name in ("coire-edge-a", "coire-edge-b"):
            node = NodeRow(
                id=uuid.uuid4(),
                name=name,
                role="studio",
                memory_total_bytes=16 * 1024**3,
                disk_total_bytes=100 * 1024**3,
                agent_version="fixture",
            )
            session.add(node)
            await session.flush()
            session.add(
                VariantCopyRow(
                    variant_id=source.binding.variant_id,
                    node_id=node.id,
                    path="/synthetic",
                    bytes=100,
                    manifest_sha256=DIGEST,
                    verified=True,
                    role="origin",
                )
            )
            session.add(
                NodeMemoryLedgerRow(
                    node_id=node.id,
                    budget_bytes=8 * 1024**3,
                    sandbox_bytes=1024**3,
                    measured_resident_bytes=0,
                    health="healthy",
                    health_sampled_at=datetime.now(UTC),
                    thermal_state="nominal",
                )
            )
            session.add(
                MemoryReservationRow(
                    node_id=node.id,
                    holder_type="sandbox",
                    holder_id="agent-sandbox",
                    bytes=1024**3,
                    pinned=True,
                    state="held",
                )
            )
        for source in dispatch.sources:
            session.add(
                TrainingDatasetRevisionRow(
                    id=source.binding.dataset_id,
                    owner_user_id=row.owner_user_id,
                    name="fixture",
                    format=source.binding.format.value,
                    state="ready",
                    source_sha256=source.binding.source_sha256,
                    source_bytes=(tmp_path / f"{source.binding.dataset_id}.jsonl").stat().st_size
                    if (tmp_path / f"{source.binding.dataset_id}.jsonl").exists()
                    else 100,
                    storage_key=str(source.binding.dataset_id),
                    provenance={"source": "fixture", "license_note": "test"},
                    row_count=source.analysis.row_count,
                    split_seed=0,
                    validation_fraction=0.5,
                    split_manifest=source.split.model_dump(mode="json"),
                    split_sha256=source.binding.split_sha256,
                )
            )
            await session.flush()
            command_id = uuid.uuid4()
            session.add(
                TrainingCommandRow(
                    id=command_id,
                    actor_user_id=row.owner_user_id,
                    idempotency_key=f"analysis-fixture:{source.binding.dataset_id}",
                    operation="dataset.analyze",
                    subject_id=str(source.binding.dataset_id),
                    request_sha256=payload_digest(source.binding),
                    payload={"analysis": source.binding.model_dump(mode="json")},
                )
            )
            session.add(
                TrainingDatasetAnalysisRow(
                    id=source.analysis.id,
                    dataset_id=source.binding.dataset_id,
                    model_id=source.binding.model_id,
                    variant_id=source.binding.variant_id,
                    command_id=command_id,
                    identity_sha256=payload_digest(source.binding),
                    state="succeeded",
                    result=source.analysis.model_dump(mode="json"),
                )
            )
        row.state = "queued"
        session.add(row)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.parametrize("inconclusive", [False, True])
async def test_audited_idempotency_and_atomic_competing_probe_admission(
    measurement_db: async_sessionmaker[AsyncSession],
    inconclusive: bool,
) -> None:
    factory = measurement_db
    settings = Settings(training_enabled=True)
    async with factory.begin() as session:
        fixture = await session.scalar(select(TrainingMeasurementRow))
        assert fixture is not None
        principal = Principal(
            kind=PrincipalKind.USER, user_id=fixture.owner_user_id, role=UserRole.ADMIN
        )
        request = TrainingMeasurementRequest.model_validate(fixture.request)
        first = await submit_measurement(session, principal, request, "one", settings=settings)
        second = await submit_measurement(session, principal, request, "two", settings=settings)
    async with factory.begin() as session:
        replay = await submit_measurement(session, principal, request, "one", settings=settings)
        assert replay == first
        changed = request.model_copy(deep=True)
        changed.spec.optim.updates += 1
        with pytest.raises(TrainingConflict):
            await submit_measurement(session, principal, changed, "one", settings=settings)

    async def admit(identity: uuid.UUID) -> TrainingMeasurementDispatch | None:
        async with factory.begin() as session:
            row = await session.get(TrainingMeasurementRow, identity, with_for_update=True)
            assert row is not None
            binding = await freeze_measurement_inputs(session, request)
            return await admit_measurement(session, row, binding)

    admissions = await asyncio.gather(admit(first.measurement_id), admit(second.measurement_id))
    assert sum(d is not None for d in admissions) == 1
    winner = next(d for d in admissions if d is not None)
    async with factory.begin() as session:
        hold = await session.get(MemoryReservationRow, winner.commands[0].prepare.reservation_id)
        assert hold is not None and hold.bytes == 7 * 1024**3
        command = await session.scalar(
            select(TrainingCommandRow).where(
                TrainingCommandRow.operation == "training.measurement",
                TrainingCommandRow.subject_id == str(winner.commands[0].measurement_id),
            )
        )
        assert command is not None
        command.payload = {**command.payload, "dispatch": winner.model_dump(mode="json")}
        delivery = await mint_measurement_inputs(
            session, principal, winner.commands[0], settings=settings
        )
        secret = delivery.sources[0].grant.secret
        assert secret not in str(command.payload)
    async with factory.begin() as session:
        source = await authorized_measurement_source(
            session, request.spec.data.train.datasets[0].dataset_id, "coire-edge-a", secret
        )
        assert source.source_sha256 == DIGEST
        with pytest.raises(TrainingNotFound):
            await authorized_measurement_source(session, source.id, "coire-edge-b", secret)
        grant = await session.scalar(
            select(TrainingCommandRow).where(
                TrainingCommandRow.operation == "measurement.input.grant"
            )
        )
        assert grant is not None
        grant.payload = {
            **grant.payload,
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
    async with factory.begin() as session:
        with pytest.raises(TrainingNotFound):
            await authorized_measurement_source(
                session, request.spec.data.train.datasets[0].dataset_id, "coire-edge-a", secret
            )
    # Synthetic CPU observations exercise persistence only, never hardware acceptance.
    client = MeasurementNodeClient(settings)
    executor = TrainingMeasurementExecutor(settings, client)
    async with factory.begin() as session:
        winner_row = await session.get(TrainingMeasurementRow, winner.commands[0].measurement_id)
        assert winner_row is not None
        report = build_report(winner_row, winner, [observation(winner.commands[0])], [], settings)
        await executor.finish(
            session, winner_row.id, principal, frozen_binding(winner), winner, report, [None]
        )
        hold = await session.get(MemoryReservationRow, winner.commands[0].prepare.reservation_id)
        assert hold is not None and hold.state.value == "held"
        assert winner_row.state == "running"
        assert await session.scalar(select(TrainingProfileRow.id)) is None
    async with factory.begin() as session:
        winner_row = await session.get(TrainingMeasurementRow, winner.commands[0].measurement_id)
        assert winner_row is not None
        proof = TrainingStopReceipt(
            attempt_id=winner.commands[0].prepare.attempt_id,
            fence=1,
            node="coire-edge-a",
            pid=None if inconclusive else 123,
            process_create_time=None if inconclusive else 1.0,
            stopped=True,
            observed_at=datetime.now(UTC),
        )
        await executor.finish(
            session,
            winner_row.id,
            principal,
            frozen_binding(winner),
            winner,
            None if inconclusive else report,
            [proof],
        )
        assert winner_row.state == ("inconclusive" if inconclusive else "succeeded")
        assert (await session.scalar(select(TrainingProfileRow.id)) is not None) is not inconclusive
        command = await session.scalar(
            select(TrainingCommandRow).where(
                TrainingCommandRow.operation == "training.measurement",
                TrainingCommandRow.subject_id == str(winner_row.id),
            )
        )
        assert command is not None and command.state == ("failed" if inconclusive else "succeeded")
        assert command.payload["stop_proofs"] == [proof.model_dump(mode="json")]
    await client.aclose()


@pytest.mark.parametrize("measurement_db", ["mixture"], indirect=True)
async def test_measurement_freezes_all_sources_replacement_quotas_and_independent_validation(
    measurement_db: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(training_enabled=True)
    async with measurement_db.begin() as session:
        fixture = await session.scalar(select(TrainingMeasurementRow))
        assert fixture is not None
        request = TrainingMeasurementRequest.model_validate(fixture.request)
        principal = Principal(
            kind=PrincipalKind.USER, user_id=fixture.owner_user_id, role=UserRole.ADMIN
        )
        request.spec.data.train.replacement = True
        request.spec.data.train.epoch_samples = 12
        binding = await freeze_measurement_inputs(session, request)
        assert len(binding.sources) == 3
        assert request.spec.data.validation.dataset_ids[0] not in {
            d.dataset_id for d in request.spec.data.train.datasets
        }
        receipt = await submit_measurement(
            session, principal, request, "replacement-matrix", settings=settings
        )
        row = await session.get(TrainingMeasurementRow, receipt.measurement_id)
        assert row is not None
        dispatch = await admit_measurement(session, row, binding)
        assert dispatch is not None and len(dispatch.sources) == 3
        assert len(dispatch.commands[0].prepare.resolved.datasets) == 3
        persisted = await session.scalar(
            select(TrainingCommandRow).where(
                TrainingCommandRow.operation == "training.measurement",
                TrainingCommandRow.subject_id == str(row.id),
            )
        )
        assert persisted is not None
        persisted.payload = {**persisted.payload, "dispatch": dispatch.model_dump(mode="json")}
        delivery = await mint_measurement_inputs(
            session, principal, dispatch.commands[0], settings=settings
        )
        assert len(delivery.sources) == 3
        assert {s.grant.dataset_id for s in delivery.sources} == {
            s.binding.dataset_id for s in binding.sources
        }
        unsupported_quota = request.model_copy(deep=True)
        unsupported_quota.spec.data.train.replacement = False
        unsupported_quota.spec.data.train.epoch_samples = 6
        unsupported_quota.spec.data.train.datasets[0].mixture_proportion = 0.99
        unsupported_quota.spec.data.train.datasets[1].mixture_proportion = 0.01
        with pytest.raises(TrainingValidationError, match="quota"):
            await freeze_measurement_inputs(session, unsupported_quota)


@pytest.mark.parametrize("measurement_db", ["mixture"], indirect=True)
async def test_missing_rank_measurement_hook_is_refused_before_queueing(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coire_api.nodes_client import NodeClient
    from coire_scheduler.training_guard import hardware_digest

    async with measurement_db.begin() as session:
        row = await session.scalar(select(TrainingMeasurementRow))
        assert row is not None
        nodes = {n.name: n for n in await session.scalars(select(NodeRow))}

        async def capability(
            _client: NodeClient, method: str, node_name: str, path: str, **kwargs: object
        ) -> tuple[int, object]:
            assert method == "GET" and path == "/node/training/measurements/capabilities"
            return 200, TrainingMeasurementCapabilities(
                node=node_name,
                hardware_sha256=hardware_digest(nodes[node_name]),
                world_sizes=[1],
                measurement_checkpoint=False,
            ).model_dump(mode="json")

        monkeypatch.setattr(NodeClient, "_call", capability)
        request = TrainingMeasurementRequest.model_validate(row.request)
        request.spec.placement.mode = "data_parallel"
        request.nodes = ["coire-edge-a", "coire-edge-b"]
        principal = Principal(
            kind=PrincipalKind.USER, user_id=row.owner_user_id, role=UserRole.ADMIN
        )
        before = await session.scalar(select(func.count()).select_from(TrainingMeasurementRow))
        with pytest.raises(TrainingUnavailable, match="callback"):
            await submit_measurement(
                session,
                principal,
                request,
                "unavailable-rank-probe",
                settings=Settings(training_enabled=True),
            )
        assert (
            await session.scalar(select(func.count()).select_from(TrainingMeasurementRow)) == before
        )


@pytest.mark.parametrize("measurement_db", ["mixture"], indirect=True)
async def test_two_rank_admission_is_atomic_and_reserves_the_same_full_slot_on_both(
    measurement_db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    async def cpu_link_projection(*args: object) -> SimpleNamespace:
        # Control-flow fixture only, not a measured JACCL or numerical pass.
        return SimpleNamespace(tp_eligible=True)

    monkeypatch.setattr("coire_api.sharding.link_projection", cpu_link_projection)
    hostfile = tmp_path / "generated-fixture.json"
    hostfile.write_text(
        json.dumps(
            {
                "backend": "jaccl",
                "hosts": [
                    {"ssh": "coire-edge-a.fabric", "rdma": [None, "cpu-fixture-device"]},
                    {"ssh": "coire-edge-b.fabric", "rdma": ["cpu-fixture-device", None]},
                ],
            }
        )
    )
    settings = Settings(training_enabled=True, sharding_jaccl_hostfile=str(hostfile))
    async with measurement_db.begin() as session:
        row = await session.scalar(select(TrainingMeasurementRow))
        assert row is not None
        request = TrainingMeasurementRequest.model_validate(row.request)
        request.spec.placement.mode = "data_parallel"
        request.nodes = ["coire-edge-a", "coire-edge-b"]
        row.request = request.model_dump(mode="json")
        nodes = {n.name: n for n in await session.scalars(select(NodeRow))}
        for node in nodes.values():
            node.data_host = node.name + ".fabric"
        ledger = await session.get(NodeMemoryLedgerRow, nodes["coire-edge-b"].id)
        assert ledger is not None
        ledger.budget_bytes = 5 * 1024**3
        block = MemoryReservationRow(
            node_id=ledger.node_id,
            holder_type="training",
            holder_id="other-rank",
            bytes=1024,
            pinned=True,
            state="held",
        )
        session.add(block)
        await session.flush()
        binding = await freeze_measurement_inputs(session, request)
        assert await admit_measurement(session, row, binding, settings=settings) is None
        assert (
            await session.scalar(
                select(MemoryReservationRow.id).where(
                    MemoryReservationRow.holder_id == f"measurement:{row.id}"
                )
            )
            is None
        )
        block.state = MemoryReservationState.RELEASED
        await session.flush()
        dispatch = await admit_measurement(session, row, binding, settings=settings)
        assert dispatch is not None and len(dispatch.commands) == 2
        p0, p1 = [c.prepare for c in dispatch.commands]
        assert p0.attempt_id == p1.attempt_id and p0.job_id == p1.job_id
        assert p0.resolved == p1.resolved and p0.collective == p1.collective
        assert p0.collective is not None and p0.collective.runtime_sha256 == binding.runtime_sha256
        assert p0.resolved.resource_envelope.memory_bytes == 4 * 1024**3
        holds = list(
            await session.scalars(
                select(MemoryReservationRow).where(
                    MemoryReservationRow.holder_id == f"measurement:{row.id}"
                )
            )
        )
        assert len(holds) == 2 and all(h.bytes == 4 * 1024**3 for h in holds)


@pytest.mark.parametrize("measurement_db", ["single", "mixture"], indirect=True)
async def test_public_resolution_validates_sampling_without_inventing_resource_evidence(
    measurement_db: async_sessionmaker[AsyncSession],
) -> None:
    import yaml

    from coire_api.training.specs import resolve_submission
    from coire_core.models.training import TrainingPlacement, TrainingSubmission

    async with measurement_db.begin() as session:
        row = await session.scalar(select(TrainingMeasurementRow))
        assert row is not None
        spec = TrainingMeasurementRequest.model_validate(row.request).spec
        spec.placement = TrainingPlacement(mode="data_parallel")
        spec.optim.batch_size = 2
        spec.data.train.replacement = True
        if spec.data.train.epoch_samples % 2:
            spec.data.train.epoch_samples += 1
        submission = TrainingSubmission(source_yaml=yaml.safe_dump(spec.model_dump(mode="json")))
        result = await resolve_submission(
            session, submission, settings=Settings(training_enabled=True)
        )
        assert result.resolved is None and result.reasons == ["profile_missing"]
        # Even valid native sampling never authorizes a caller-estimated load.
        assert (
            await session.scalar(
                select(MemoryReservationRow.id).where(
                    MemoryReservationRow.holder_type == "training"
                )
            )
            is None
        )
