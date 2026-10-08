"""Real-Postgres schema constraints and guarded downgrade for feature 016."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    Base,
    ModelRow,
    ModelVariantRow,
    TrainingAttemptRow,
    TrainingEventRow,
    UserRow,
)
from coire_api.training.events import append_event, metric_page, record_metric, replay_events
from coire_api.training.service import begin_command, record_receipt
from coire_core.errors import TrainingConflict
from coire_core.models.auth import UserRole
from coire_core.models.training import (
    TrainingCommandReceipt,
    TrainingControlRequest,
    TrainingJobState,
    TrainingMetricSample,
    TrainingStateEvent,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 for disposable Postgres training schema tests",
    ),
]

JOB = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
ALEMBIC_CONFIG = Path(__file__).resolve().parents[2] / "alembic.ini"


def column_names(connection: Connection, table_name: str) -> set[str]:
    return {str(column["name"]) for column in inspect(connection).get_columns(table_name)}


@pytest.fixture
async def connection(training_postgres_url: str) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(training_postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()


async def seed_job(
    conn: AsyncConnection, *, job_id: str = JOB, name: str = "sft-test", key: str = "submit-1"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner, model, variant = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # Source rows use their actual schema; only generated test identities are written.
    await conn.execute(
        insert(UserRow).values(
            id=owner,
            email=f"{owner}@training.test",
            display_name="Training admin",
            role="admin",
            active=True,
        )
    )
    await conn.execute(
        insert(ModelRow).values(
            id=model,
            repo_id=f"synthetic/{model}",
            slug=f"synthetic--{model}",
            display_name="Training base",
            state="ready",
            visibility="admin_only",
            placement_policy="single:auto",
            memory_estimate_bytes=1024,
            idle_ttl_seconds=900,
            precision="bf16",
            weight_bytes=1024,
            total_bytes=1024,
            file_count=2,
        )
    )
    await conn.execute(
        insert(ModelVariantRow).values(
            id=variant,
            model_id=model,
            name="test",
            slug=f"synthetic--{variant}",
            precision="bf16",
            state="ready",
            memory_estimate_bytes=1024,
            source_revision="local-fixture",
        )
    )
    await conn.execute(
        text(
            "INSERT INTO training_jobs (id,owner_user_id,model_id,base_variant_id,idempotency_key,output_slug,source_yaml,source_sha256,intent_sha256,submitted_spec,state,queue_deadline_at,execution_deadline_at) VALUES (:id,:owner,:model,:variant,:key,:slug,'{}',:hash,:hash,'{}','queued',:deadline,:deadline)"
        ),
        {
            "id": job_id,
            "owner": owner,
            "model": model,
            "variant": variant,
            "key": key,
            "slug": name,
            "hash": "a" * 64,
            "deadline": datetime.now(UTC) + timedelta(hours=1),
        },
    )
    await conn.commit()
    return owner, model, variant


async def test_training_schema_exists_and_legacy_usage_has_nullable_target(
    connection: AsyncConnection,
) -> None:
    names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    assert {
        "training_jobs",
        "training_attempts",
        "training_participants",
        "training_checkpoints",
        "training_artifact_copies",
        "training_adapters",
        "training_events",
        "training_metrics",
        "training_dataset_revisions",
        "training_dataset_analyses",
        "training_profiles",
    } <= set(names)
    columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("usage_records"))
    targets = {column["name"]: column for column in columns}
    assert targets["variant_id"]["nullable"] and targets["adapter_id"]["nullable"]


async def test_job_idempotency_and_output_namespace_cannot_duplicate(
    connection: AsyncConnection,
) -> None:
    await seed_job(connection)
    for changed in ("idempotency_key", "output_slug"):
        original = await connection.execute(
            text("SELECT * FROM training_jobs WHERE id=:id"), {"id": JOB}
        )
        data = dict(original.mappings().one())
        data["id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
        data[changed] = "different"
        with pytest.raises(IntegrityError):
            await connection.execute(insert(Base.metadata.tables["training_jobs"]).values(**data))
        await connection.rollback()


async def test_attempt_fences_and_event_sequences_are_unique(connection: AsyncConnection) -> None:
    await seed_job(connection)
    parameters = {
        "id": JOB,
        "job": JOB,
        "hash": "a" * 64,
        "expires": datetime.now(UTC) + timedelta(seconds=30),
    }
    statement = text(
        "INSERT INTO training_attempts (id,job_id,generation,fence,world_size,runtime_sha256,state,lease_expires_at) VALUES (:id,:job,1,1,1,:hash,'preparing',:expires)"
    )
    await connection.execute(statement, parameters)
    await connection.commit()
    with pytest.raises(IntegrityError):
        await connection.execute(statement, {**parameters, "id": "01ARZ3NDEKTSV4RRFFQ69G5FAW"})
    await connection.rollback()


async def test_final_adapter_identity_cannot_be_overwritten(connection: AsyncConnection) -> None:
    _owner, model, variant = await seed_job(connection)
    statement = text(
        "INSERT INTO training_adapters (id,model_id,base_variant_id,source_job_id,slug,selector,base_manifest_sha256,resolved_spec_sha256,parameterization,objective,state,visibility) VALUES (:id,:model,:variant,:job,'test',:selector,:hash,:hash,'lora','sft','validating','admin_only')"
    )
    parameters = {
        "id": uuid.uuid4(),
        "model": model,
        "variant": variant,
        "job": JOB,
        "selector": f"{model}@test",
        "hash": "a" * 64,
    }
    await connection.execute(statement, parameters)
    await connection.commit()
    with pytest.raises(IntegrityError):
        await connection.execute(statement, {**parameters, "id": uuid.uuid4()})
    await connection.rollback()


async def test_actual_revision_upgrade_downgrade_guard_and_roundtrip(
    connection: AsyncConnection, training_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = make_url(training_postgres_url)
    assert url.database == "training_test" and url.host == "127.0.0.1" and url.password is not None
    # This is the asserted disposable database. Migration fixtures must clear
    # stamps, enums and trigger functions as well as metadata-owned tables.
    await connection.execute(text("DROP SCHEMA public CASCADE"))
    await connection.execute(text("CREATE SCHEMA public"))
    await connection.commit()
    for name, value in {
        "POSTGRES_HOST": url.host,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_USER": url.username or "postgres",
        "POSTGRES_DB": url.database,
        "POSTGRES_PASSWORD": url.password,
    }.items():
        monkeypatch.setenv(name, value)
    config = Config(str(ALEMBIC_CONFIG))
    await asyncio.to_thread(command.upgrade, config, "0030_image_output_retention")
    legacy_id = uuid.uuid4()
    now = datetime.now(UTC)
    await connection.execute(
        text(
            "INSERT INTO usage_records (id,request_id,principal_kind,requested_model_id,protocol,prompt_tokens,completion_tokens,duration_ms,outcome,started_at,finished_at) VALUES (:id,:id,'api_key','legacy-model','openai',2,1,1,'succeeded',:now,:now)"
        ),
        {"id": legacy_id, "now": now},
    )
    await connection.commit()
    await asyncio.to_thread(command.upgrade, config, "0031_sft_training")
    legacy_target = await connection.execute(
        text("SELECT variant_id,adapter_id FROM usage_records WHERE id=:id"), {"id": legacy_id}
    )
    assert legacy_target.one() == (None, None)
    names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    assert "training_jobs" in names
    for name, table in Base.metadata.tables.items():
        if name.startswith("training_") and name != "training_evaluation_triggers":
            columns = await connection.run_sync(column_names, name)
            # Feature 0031 is frozen; additive 0032 columns are tested by its own migration.
            later_columns = {
                "training_jobs": {"evaluation_pause_trigger_id"},
                "training_adapters": {"purpose", "evaluation_trigger_id"},
            }.get(name, set())
            assert columns == set(table.columns.keys()) - later_columns
    await seed_job(connection)
    with pytest.raises(RuntimeError, match="live training"):
        await asyncio.to_thread(command.downgrade, config, "0030_image_output_retention")
    assert (
        await connection.scalar(
            text("SELECT source_yaml FROM training_jobs WHERE id=:id"), {"id": JOB}
        )
        == "{}"
    )
    await connection.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        await connection.execute(
            text("UPDATE training_jobs SET source_yaml='changed' WHERE id=:id"), {"id": JOB}
        )
    await connection.rollback()
    await connection.execute(text("DELETE FROM training_jobs WHERE id=:id"), {"id": JOB})
    await connection.commit()
    await asyncio.to_thread(command.downgrade, config, "0030_image_output_retention")
    names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    assert not any(name.startswith("training_") for name in names)
    await connection.rollback()
    await asyncio.to_thread(command.upgrade, config, "0031_sft_training")
    assert (
        await connection.scalar(text("SELECT version_num FROM alembic_version"))
        == "0031_sft_training"
    )


async def test_command_retry_conflict_and_audit_are_transactional(
    connection: AsyncConnection,
) -> None:
    user_id = uuid.uuid4()
    await connection.execute(
        insert(UserRow).values(
            id=user_id,
            email=f"{user_id}@training.test",
            display_name="Admin",
            role=UserRole.ADMIN,
            active=True,
        )
    )
    await connection.commit()
    principal = Principal(
        kind=PrincipalKind.USER, user_id=user_id, role=UserRole.ADMIN, subject="training-admin"
    )
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        async with session.begin():
            command_row = await begin_command(
                session,
                principal,
                operation="training.pause",
                subject_id=JOB,
                idempotency_key="pause-1",
                request=TrainingControlRequest(expected_version=1),
            )
            command_id = command_row.id
            await record_receipt(
                session,
                principal,
                command_row,
                TrainingCommandReceipt(
                    command_id=command_id, job_id=JOB, state=TrainingJobState.PAUSING, version=2
                ),
            )
        async with session.begin():
            replay = await begin_command(
                session,
                principal,
                operation="training.pause",
                subject_id=JOB,
                idempotency_key="pause-1",
                request=TrainingControlRequest(expected_version=1),
            )
            assert replay.id == command_id and replay.receipt is not None
        with pytest.raises(TrainingConflict):
            async with session.begin():
                await begin_command(
                    session,
                    principal,
                    operation="training.pause",
                    subject_id=JOB,
                    idempotency_key="pause-1",
                    request=TrainingControlRequest(expected_version=2),
                )
    assert (
        await connection.scalar(
            text("SELECT count(*) FROM audit_log WHERE action='training.pause'")
        )
        == 1
    )


async def test_mandatory_audit_failure_rolls_back_the_command(
    connection: AsyncConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid.uuid4()
    await connection.execute(
        insert(UserRow).values(
            id=user_id,
            email=f"{user_id}@training.test",
            display_name="Admin",
            role=UserRole.ADMIN,
            active=True,
        )
    )
    await connection.commit()
    principal = Principal(
        kind=PrincipalKind.USER, user_id=user_id, role=UserRole.ADMIN, subject="training-admin"
    )

    async def fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("mandatory audit unavailable")

    monkeypatch.setattr("coire_api.training.service.write_principal_audit", fail_audit)
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        with pytest.raises(RuntimeError, match="mandatory audit"):
            async with session.begin():
                command_row = await begin_command(
                    session,
                    principal,
                    operation="training.pause",
                    subject_id=JOB,
                    idempotency_key="pause-1",
                    request=TrainingControlRequest(expected_version=1),
                )
                await record_receipt(
                    session,
                    principal,
                    command_row,
                    TrainingCommandReceipt(
                        command_id=command_row.id,
                        job_id=JOB,
                        state=TrainingJobState.PAUSING,
                        version=2,
                    ),
                )
    assert await connection.scalar(text("SELECT count(*) FROM training_commands")) == 0


async def test_event_replay_and_expired_cursor_snapshot_are_content_free(
    connection: AsyncConnection,
) -> None:
    await seed_job(connection)
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        for expected in (1, 2):
            event = await append_event(
                session, JOB, TrainingStateEvent(kind="state", state=TrainingJobState.QUEUED)
            )
            assert event.id == expected
        await session.commit()
        page = await replay_events(session, JOB)
        assert [event.id for event in page.events] == [1, 2] and page.cursor == 2
        await session.execute(
            delete(TrainingEventRow).where(
                TrainingEventRow.job_id == JOB, TrainingEventRow.sequence == 1
            )
        )
        await session.commit()
        reset = await replay_events(session, JOB)
        assert reset.reset is not None and reset.cursor == 2 and reset.events == []
        assert "source_yaml" not in reset.reset.model_dump()
        with pytest.raises(TrainingConflict):
            await replay_events(session, JOB, after=3)


async def test_metrics_deduplicate_and_reject_stale_attempts(connection: AsyncConnection) -> None:
    await seed_job(connection)
    await connection.execute(
        text("UPDATE training_jobs SET state='running',fence=1 WHERE id=:id"), {"id": JOB}
    )
    await connection.execute(
        insert(TrainingAttemptRow).values(
            id=JOB,
            job_id=JOB,
            generation=1,
            fence=1,
            world_size=1,
            runtime_sha256="a" * 64,
            state="running",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )
    )
    await connection.commit()
    sample = TrainingMetricSample(
        job_id=JOB,
        attempt_id=JOB,
        update=1,
        kind="train",
        loss=1.25,
        learning_rate=0.01,
        tokens=4,
        tokens_per_second=2,
        updates_per_second=1,
        footprint_bytes=1024,
        peak_bytes=2048,
        recorded_at=datetime.now(UTC),
    )
    async with AsyncSession(bind=connection, expire_on_commit=False) as session:
        metric = await record_metric(session, sample, fence=1)
        assert (await record_metric(session, sample, fence=1)).id == metric.id
        await session.commit()
        page = await metric_page(session, JOB)
        assert len(page.items) == 1 and page.items[0].loss == 1.25
        replay = await replay_events(session, JOB)
        assert len(replay.events) == 1 and replay.events[0].kind == "progress"
        with pytest.raises(TrainingConflict):
            await record_metric(session, sample.model_copy(update={"loss": 2.5}), fence=1)
        with pytest.raises(TrainingConflict):
            await record_metric(session, sample, fence=2)
