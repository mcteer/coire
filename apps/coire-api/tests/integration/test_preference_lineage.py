"""Actual Postgres ancestry publication and retired-parent snapshot retention."""

import uuid

import pytest
from evaluation_training_fixtures import seed_evaluated_training
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from coire_api.db import Base, TrainingAdapterRow, TrainingJobRow
from coire_api.training.lineage import get_lineage, persist_lineage
from coire_api.training.service import payload_digest
from coire_core.errors import TrainingConflict
from coire_core.models.adapters import InferenceTarget
from coire_core.models.training import ResolvedTrainingSpecV3

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("objective", ["dpo", "orpo"])
async def test_chained_ancestry_is_immutable_after_parent_retirement(
    training_postgres_url: str, objective: str
) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, parent_id = await seed_evaluated_training(session, version=3)
            job = await session.get(TrainingJobRow, job_id)
            parent = await session.get(TrainingAdapterRow, parent_id)
            assert job is not None and parent is not None
            bare = ResolvedTrainingSpecV3.model_validate(job.resolved_spec)
            parent.objective = "dpo"
            await persist_lineage(session, parent, bare)
            target = InferenceTarget(
                model_id=parent.model_id,
                variant_id=parent.base_variant_id,
                base_manifest_sha256=parent.base_manifest_sha256,
                adapter_id=parent.id,
                adapter_manifest_sha256=parent.manifest_sha256,
            )
            raw = bare.model_dump(mode="json")
            raw["spec"].update(
                init_adapter=str(parent_id),
                objective=objective,
                objective_options={"beta": 0.1} if objective == "dpo" else {"weight": 0.1},
            )
            raw.update(
                initial_target=target.model_dump(mode="json"),
                reference_target=target.model_dump(mode="json") if objective == "dpo" else None,
            )
            raw["resource_envelope"].update(
                reference_weight_bytes=1 if objective == "dpo" else 0,
                reference_adapter_bytes=1 if objective == "dpo" else 0,
            )
            resolved = ResolvedTrainingSpecV3.model_validate(raw)
            child = TrainingAdapterRow(
                id=uuid.uuid4(),
                model_id=parent.model_id,
                base_variant_id=parent.base_variant_id,
                source_job_id=parent.source_job_id,
                source_checkpoint_id=parent.source_checkpoint_id,
                slug="lineage-child",
                selector=f"{parent.model_id}@lineage-child",
                base_manifest_sha256=parent.base_manifest_sha256,
                manifest_sha256="d" * 64,
                resolved_spec_sha256=payload_digest(resolved),
                parameterization=parent.parameterization,
                objective=objective,
                state="ready",
                visibility="admin_only",
                verified=False,
                metadata_record={},
            )
            session.add(child)
            await persist_lineage(session, child, resolved)
            snapshot = await get_lineage(session, child.id)
            assert snapshot.parent == target and len(snapshot.ancestors) == 1
            assert snapshot.ancestors[0].objective == "dpo"
            assert snapshot.ancestors[0].dataset_ids == [item.dataset_id for item in bare.datasets]
            parent.state = "retired"
            await session.commit()
            assert await get_lineage(session, child.id) == snapshot
            assert not child.verified and child.visibility == "admin_only"
            await persist_lineage(session, child, resolved)
            changed = resolved.model_copy(
                update={
                    "initial_target": bare.initial_target,
                    "reference_target": bare.initial_target if objective == "dpo" else None,
                }
            )
            with pytest.raises(TrainingConflict, match="immutable"):
                await persist_lineage(session, child, changed)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "state,pinned",
    [("queued", True), ("running", True), ("succeeded", False), ("inconclusive", False)],
)
async def test_initial_parent_pin_tracks_measurement_intent(
    training_postgres_url: str, state: str, pinned: bool
) -> None:
    from coire_api.auth import Principal
    from coire_api.db import TrainingMeasurementRow
    from coire_api.training.adapters import curate_adapter
    from coire_api.training.preference_specs import initial_adapter_pinned
    from coire_core.models.adapters import AdapterRetireRequest

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, parent_id = await seed_evaluated_training(session, version=3, state="succeeded")
            job = await session.get(TrainingJobRow, job_id)
            parent = await session.get(TrainingAdapterRow, parent_id)
            assert job is not None and parent is not None
            resolved = ResolvedTrainingSpecV3.model_validate(job.resolved_spec)
            spec = resolved.spec.model_copy(update={"init_adapter": parent_id})
            session.add(
                TrainingMeasurementRow(
                    owner_user_id=job.owner_user_id,
                    request={"spec": spec.model_dump(mode="json")},
                    state=state,
                )
            )
            await session.flush()
            assert await initial_adapter_pinned(session, parent_id) is pinned
            if pinned:
                with pytest.raises(TrainingConflict, match="pinned"):
                    await curate_adapter(
                        session,
                        Principal.model_validate(job.authorization_snapshot),
                        parent_id,
                        AdapterRetireRequest(expected_version=parent.version),
                        str(uuid.uuid4()),
                    )
                assert parent.state == "ready"
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "state,pinned",
    [
        ("queued", True),
        ("running", True),
        ("paused", True),
        ("recovering", True),
        ("succeeded", False),
        ("failed", False),
        ("cancelled", False),
    ],
)
async def test_initial_parent_pin_tracks_resumable_job_metadata(
    training_postgres_url: str, state: str, pinned: bool
) -> None:
    from coire_api.training.preference_specs import initial_adapter_pinned

    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            job_id, parent_id = await seed_evaluated_training(session, version=3, state=state)
            job = await session.get(TrainingJobRow, job_id)
            assert job is not None and job.resolved_spec is not None
            raw = ResolvedTrainingSpecV3.model_validate(job.resolved_spec).model_dump(mode="json")
            raw["spec"]["init_adapter"] = str(parent_id)
            raw["initial_target"].update(
                adapter_id=str(parent_id), adapter_manifest_sha256="c" * 64
            )
            raw["reference_target"] = raw["initial_target"]
            raw["resource_envelope"]["reference_adapter_bytes"] = 1
            job.resolved_spec = ResolvedTrainingSpecV3.model_validate(raw).model_dump(mode="json")
            await session.flush()
            assert await initial_adapter_pinned(session, parent_id) is pinned
            if pinned:
                from coire_api.auth import Principal
                from coire_api.training.adapters import curate_adapter
                from coire_core.models.adapters import AdapterRetireRequest

                parent = await session.get(TrainingAdapterRow, parent_id)
                assert parent is not None
                with pytest.raises(TrainingConflict, match="pinned"):
                    await curate_adapter(
                        session,
                        Principal.model_validate(job.authorization_snapshot),
                        parent_id,
                        AdapterRetireRequest(expected_version=parent.version),
                        str(uuid.uuid4()),
                    )
                assert parent.state == "ready"
    finally:
        await engine.dispose()
