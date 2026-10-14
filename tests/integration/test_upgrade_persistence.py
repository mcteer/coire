"""Real PostgreSQL fencing; no mocked database can prove concurrent ownership."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    AuditRow,
    Base,
    ChatFileProcessingRow,
    NodeRow,
    UpgradeOperationRow,
    UpgradeReceiptRow,
    UserRow,
)
from coire_api.upgrades.events import event_page
from coire_api.upgrades.grants import consume, issue
from coire_api.upgrades.inventory import job_inventory, privacy_inventory
from coire_api.upgrades.persistence import accept, append_receipt, register_release
from coire_api.upgrades.schema_identity import inspect_schema
from coire_api.upgrades.service import ingest_receipt
from coire_core.errors import UpgradeConflict, UpgradeForbidden
from coire_core.models.auth import UserRole
from coire_core.models.node import NodeRole
from coire_core.models.upgrades import (
    UpgradeActivationRequest,
    UpgradeCompatibility,
    UpgradeEvidence,
    UpgradeExecutionPlan,
    UpgradeKind,
    UpgradeMaintenance,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeRelease,
    UpgradeReleaseManifest,
    UpgradeStartRequest,
)
from coire_scheduler.maintenance import allows_admission, fence, release_verified


def release(owner: object, label: str, sources: tuple[str, ...]) -> UpgradeRelease:
    manifest = UpgradeReleaseManifest(
        kind=UpgradeKind.STUDIO,
        version=label,
        source_revision="a" * 40,
        platform="macos-arm64",
        prepared_at=datetime.now(UTC),
        compatibility=UpgradeCompatibility(
            source_sha256s=sources, readable_schema_heads=("0034_upgrades",)
        ),
        artifact_sha256s={"wheel": "a" * 64},
        staged_bytes=1024,
        environment_id=label,
        environment_sha256="a" * 64,
    )
    return UpgradeRelease.model_validate(
        {
            "release_id": uuid4(),
            "prepared_by": owner,
            "prepared_artifact_id": label,
            "manifest_sha256": manifest.sha256(),
            "manifest": manifest,
        }
    )


async def test_concurrent_acceptance_and_receipt_replay(training_postgres_url: str) -> None:
    schema = f"upgrade_persistence_{uuid4().hex}"
    owner_engine = create_async_engine(training_postgres_url)
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("CREATE SEQUENCE IF NOT EXISTS upgrade_generation_seq"))
            await conn.execute(
                text("CREATE TABLE alembic_version (version_num varchar(64) PRIMARY KEY)")
            )
            await conn.execute(text("INSERT INTO alembic_version VALUES ('0034_upgrades')"))
        async with sessions.begin() as session:
            assert all(
                owner.status == "complete" and not owner.entries
                for owner in await job_inventory(session)
            )
            baseline = await inspect_schema(session)
            await session.execute(
                text("CREATE TABLE upgrade_schema_probe (id integer PRIMARY KEY)")
            )
            assert await inspect_schema(session) != baseline
            await session.execute(text("DROP TABLE upgrade_schema_probe"))
            assert await inspect_schema(session) == baseline
        owner = uuid4()
        principal = Principal(kind=PrincipalKind.ADMIN, user_id=owner, role=UserRole.ADMIN)
        source = release(owner, "source", ("f" * 64,))
        target = release(owner, "target", (source.manifest_sha256,))
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@example.test",
                    display_name="Upgrade test",
                    role=UserRole.ADMIN,
                )
            )
        async with sessions.begin() as session:
            await register_release(session, principal, source)
            await register_release(session, principal, target)
        request = UpgradeStartRequest(
            release_id=target.release_id,
            expected_source_sha256=source.manifest_sha256,
            idempotency_key=uuid4(),
            acknowledge_maintenance=True,
        )

        async def submit() -> tuple[str, bool]:
            async with sessions.begin() as session:
                op, created = await accept(
                    session, principal, request, source.release_id, node="coire-edge-a"
                )
                return op.job_id, created

        results = await asyncio.gather(submit(), submit())
        assert results[0][0] == results[1][0]
        assert sum(created for _, created in results) == 1
        async with sessions.begin() as session:
            op, _ = await accept(
                session, principal, request, source.release_id, node="coire-edge-a"
            )
            with pytest.raises(UpgradeConflict):
                await accept(
                    session,
                    principal,
                    request.model_copy(update={"idempotency_key": uuid4()}),
                    source.release_id,
                    node="coire-edge-b",
                )
            receipt = UpgradeReceipt(
                job_id=op.job_id,
                generation=op.generation,
                sequence=1,
                effect_id="preflight",
                phase=UpgradePhase.PREFLIGHT,
                occurred_at=datetime.now(UTC),
                outcome="intent",
            )
            assert await append_receipt(session, receipt, principal)
            assert not await append_receipt(session, receipt, principal)
            page = await event_page(session, op.job_id, limit=1)
            assert page.cursor == 1 and len(page.events) == 1 and not page.reset
            assert (await event_page(session, op.job_id, after=1)).events == ()
            reset = await event_page(session, op.job_id, after=99)
            assert reset.reset and reset.cursor == 1 and reset.events == page.events
            with pytest.raises(UpgradeConflict):
                await append_receipt(
                    session, receipt.model_copy(update={"outcome": "failed"}), principal
                )
            assert (
                len(
                    (
                        await session.scalars(
                            select(UpgradeReceiptRow).where(UpgradeReceiptRow.job_id == op.job_id)
                        )
                    ).all()
                )
                == 1
            )
            audits = (
                await session.scalars(
                    select(AuditRow).where(
                        AuditRow.target_id == op.job_id,
                        AuditRow.action == "upgrades.stage.receipt",
                    )
                )
            ).all()
            assert len(audits) == 1
            assert audits[0].detail["sequence"] == 1
        async with sessions.begin() as session:
            row = await session.get(UpgradeOperationRow, results[0][0])
            assert row is not None and row.active_slot == 1
        node_id = uuid4()
        async with sessions.begin() as session:
            session.add(
                NodeRow(
                    id=node_id,
                    name="coire-edge-a",
                    role=NodeRole.STUDIO,
                    memory_total_bytes=1024,
                    disk_total_bytes=1024,
                    agent_version="test",
                )
            )
        async with sessions.begin() as session:
            assert await allows_admission(session, [node_id])
            barrier = UpgradeMaintenance(
                job_id=op.job_id,
                generation=op.generation,
                targets=("coire-edge-a",),
                state="draining",
                updated_at=datetime.now(UTC),
                drain_deadline_at=datetime.now(UTC) + timedelta(seconds=900),
            )
            await fence(session, barrier)
        async with sessions.begin() as session:
            assert not await allows_admission(session, [node_id])
            with pytest.raises(UpgradeConflict):
                await release_verified(session, op.job_id, op.generation)
            with pytest.raises(UpgradeConflict):
                await fence(session, barrier.model_copy(update={"generation": op.generation + 1}))
            job_id = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
            session.add(
                ChatFileProcessingRow(
                    id=job_id,
                    principal_kind="service",
                    principal_subject="file-worker",
                    request_id=uuid4(),
                    operation="extract",
                    source_key="private-sensitive-key",
                    source_sha256="a" * 64,
                    state="purging",
                    deadline_at=datetime.now(UTC),
                    expires_at=datetime.now(UTC),
                )
            )
            await session.flush()
            owners = await privacy_inventory(session)
            files = next(item for item in owners if item.owner == "files")
            assert any(
                item.work_id == job_id and item.state == "cleaning" for item in files.entries
            )
            assert "private-sensitive-key" not in files.model_dump_json()
            file_job = await session.get(ChatFileProcessingRow, job_id)
            assert file_job is not None
            file_job.expires_at = datetime.now(UTC) + timedelta(hours=1)
            file_job.state = "processed"
            await session.flush()
            owners = await privacy_inventory(session)
            assert next(item for item in owners if item.owner == "files").entries
            file_job.state = "ready"
            await session.flush()
            owners = await privacy_inventory(session)
            assert next(item for item in owners if item.owner == "files").entries == ()
            file_job.state = "purged"
            await session.flush()
            owners = await privacy_inventory(session)
            assert next(item for item in owners if item.owner == "files").entries == ()
        async with sessions.begin() as session:
            row = await session.get(UpgradeOperationRow, op.job_id)
            assert row is not None
            row.phase = "smoke_before"
            row.payload = op.model_copy(
                update={"phase": UpgradePhase.SMOKE_BEFORE, "receipt_sequence": 1}
            ).model_dump(mode="json")
            model_id, variant_id = uuid4(), uuid4()
            plan = UpgradeExecutionPlan(
                operation=op,
                source=source,
                target=target,
                maintenance=barrier,
                authorization_expires_at=datetime.now(UTC) + timedelta(seconds=30),
                model_id=model_id,
                model_variant_id=variant_id,
                model_slug="owner--tiny",
                model_manifest_sha256="b" * 64,
            )
            row.execution_plan = plan.model_dump(mode="json")
            source_smoke = UpgradeReceipt(
                job_id=op.job_id,
                generation=op.generation,
                sequence=2,
                effect_id="source-smoke",
                phase=UpgradePhase.SMOKE_BEFORE,
                occurred_at=datetime.now(UTC),
                outcome="complete",
                observed_sha256=op.source_sha256,
                evidence=UpgradeEvidence(
                    check="node",
                    generated_tokens=10,
                    cleanup_verified=True,
                    runtime_sha256="a" * 64,
                    model_id=model_id,
                    model_variant_id=variant_id,
                    model_manifest_sha256="b" * 64,
                    output_sha256="c" * 64,
                ).model_dump_json(),
            )
            await append_receipt(session, source_smoke, principal)
            activation_request = UpgradeActivationRequest(
                generation=op.generation, source_sha256=op.source_sha256
            )
            with pytest.raises(UpgradeConflict):
                await issue(session, op.job_id, activation_request)
            await fence(
                session,
                barrier.model_copy(
                    update={
                        "state": "active",
                        "acknowledged_generations": {"coire-edge-a": op.generation},
                    }
                ),
            )
            grant = await issue(
                session,
                op.job_id,
                activation_request,
            )
        async with sessions.begin() as session:
            user = await session.get(UserRow, owner)
            assert user is not None
            user.active = False
        activation = UpgradeReceipt(
            job_id=op.job_id,
            generation=op.generation,
            sequence=3,
            effect_id="runtime-activation",
            phase=UpgradePhase.ACTIVATING,
            occurred_at=datetime.now(UTC),
            outcome="intent",
        )
        async with sessions.begin() as session:
            with pytest.raises(UpgradeForbidden):
                await consume(session, grant, activation, principal)
        async with sessions.begin() as session:
            user = await session.get(UserRow, owner)
            assert user is not None
            user.active = True
        async with sessions.begin() as session:
            await consume(session, grant, activation, principal)
        async with sessions.begin() as session:
            await consume(session, grant, activation, principal)
            with pytest.raises(UpgradeConflict):
                await consume(
                    session,
                    grant,
                    activation.model_copy(update={"effect_id": "another-activation"}),
                    principal,
                )
        progress = activation.model_copy(
            update={
                "sequence": 4,
                "effect_id": "post-activation",
                "phase": UpgradePhase.VERIFYING,
                "outcome": "unknown",
            }
        )
        async with sessions.begin() as session:
            assert await ingest_receipt(session, progress, principal)
            assert not await ingest_receipt(session, progress, principal)
            with pytest.raises(UpgradeConflict):
                await ingest_receipt(
                    session,
                    progress.model_copy(
                        update={
                            "sequence": 5,
                            "phase": UpgradePhase.SUCCEEDED,
                            "outcome": "complete",
                        }
                    ),
                    principal,
                )
            row = await session.get(UpgradeOperationRow, op.job_id)
            assert row is not None and row.phase == "verifying" and row.active_slot == 1
        async with sessions.begin() as session:
            duplicate = UpgradeOperationRow(
                id="01ARZ3NDEKTSV4RRFFQ69G5FAY",
                user_id=owner,
                source_release_id=source.release_id,
                target_release_id=target.release_id,
                idempotency_key=uuid4(),
                request_sha256="a" * 64,
                generation=999,
                active_slot=1,
                phase="accepted",
                payload={},
            )
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                await session.flush()
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner_engine.dispose()


async def test_core_and_both_studios_share_one_cluster_operation_owner(
    training_postgres_url: str,
) -> None:
    from upgrade_fixtures import core_plan, plan

    schema = f"upgrade_cluster_race_{uuid4().hex}"
    owner_engine = create_async_engine(training_postgres_url)
    async with owner_engine.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner = uuid4()
    principal = Principal(kind=PrincipalKind.ADMIN, user_id=owner, role=UserRole.ADMIN)
    studio, core = plan(), core_plan()
    releases = [
        item.model_copy(
            update={
                "prepared_by": owner,
                "prepared_artifact_id": value.operation.kind.value
                + "-"
                + item.prepared_artifact_id,
            }
        )
        for value in (studio, core)
        for item in (value.source, value.target)
    ]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("CREATE SEQUENCE upgrade_generation_seq"))
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=owner,
                    email=f"{owner}@cluster-race.test",
                    display_name="Admin",
                    role=UserRole.ADMIN,
                    active=True,
                )
            )
        async with sessions.begin() as session:
            for release_value in releases:
                await register_release(session, principal, release_value)
        contenders = []
        for value, node in ((studio, "coire-edge-a"), (studio, "coire-edge-b"), (core, None)):
            request = UpgradeStartRequest(
                release_id=value.target.release_id,
                expected_source_sha256=value.source.manifest_sha256,
                idempotency_key=uuid4(),
                acknowledge_maintenance=True,
            )
            contenders.append((value, node, request))

        async def submit(index: int) -> tuple[int, str] | None:
            value, node, request = contenders[index]
            try:
                async with sessions.begin() as session:
                    accepted, created = await accept(
                        session, principal, request, value.source.release_id, node=node
                    )
                    assert created
                    return index, accepted.job_id
            except UpgradeConflict:
                return None

        results = await asyncio.gather(*(submit(index) for index in range(3)))
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        index, job_id = winners[0]
        value, node, request = contenders[index]
        async with sessions.begin() as session:
            replay, created = await accept(
                session, principal, request, value.source.release_id, node=node
            )
            assert not created and replay.job_id == job_id
            rows = (await session.scalars(select(UpgradeOperationRow))).all()
            assert len(rows) == 1 and rows[0].active_slot == 1
            stale = UpgradeReceipt(
                job_id=job_id,
                generation=replay.generation + 1,
                sequence=1,
                effect_id="stale-delivery",
                phase=UpgradePhase.PREFLIGHT,
                occurred_at=datetime.now(UTC),
                outcome="intent",
            )
            with pytest.raises(UpgradeConflict):
                await append_receipt(session, stale, principal)
            assert not (await session.scalars(select(UpgradeReceiptRow))).all()
            assert rows[0].payload == replay.model_dump(mode="json")
            assert rows[0].node_command is None and rows[0].execution_plan is None
            # A database acceptance winner is not permission to dispatch any external effect.
    finally:
        await engine.dispose()
        async with owner_engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await owner_engine.dispose()
