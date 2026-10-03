"""Private image schema safety contracts."""

from __future__ import annotations

import asyncio
import os
import runpy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from coire_api.audit import write_audit
from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    AuditRow,
    Base,
    ImageInputRow,
    ImageJobEventRow,
    ImageJobRow,
    ImageOutputRow,
    ImageQuotaRow,
)
from coire_api.images import cancellation, job_capacity, maintenance, storage
from coire_core.errors import ImageConflict, ImageQuotaExceeded
from coire_core.models.images import ImageJobSettingsSnapshot, ImageSpec
from coire_core.settings import Settings


def test_image_job_identity_and_publication_evidence_are_durable() -> None:
    table = Base.metadata.tables["image_jobs"]
    assert {
        "owner_user_id",
        "idempotency_key",
        "intent_sha256",
        "submitted_spec",
        "resolved_spec",
        "state",
        "version",
        "attempt",
        "fence",
        "cancel_requested_at",
        "receipt_state",
        "cleanup_state",
        "authorization_snapshot",
    } <= set(table.c.keys())
    assert any(
        isinstance(c, UniqueConstraint)
        and {"owner_user_id", "idempotency_key"} == {x.name for x in c.columns}
        for c in table.constraints
    )
    assert any(
        isinstance(c, CheckConstraint) and "^[0-9A-HJKMNP-TV-Z]{26}$" in str(c.sqltext)
        for c in table.constraints
    )
    assert any(
        isinstance(fk, ForeignKey) and fk.target_fullname == "users.id"
        for fk in table.c.owner_user_id.foreign_keys
    )
    assert any(
        isinstance(c, ForeignKeyConstraint)
        and {fk.target_fullname for fk in c.elements}
        == {"image_preset_revisions.preset_id", "image_preset_revisions.revision"}
        for c in table.constraints
    )


def test_output_retention_has_a_partial_publication_order_index() -> None:
    indices = {str(index.name): index for index in Base.metadata.tables["image_outputs"].indexes}
    index = indices["ix_image_outputs_retention"]
    assert list(index.columns.keys()) == ["published_at", "id"]
    assert str(index.dialect_options["postgresql"]["where"]) == (
        "state = 'published' AND deleted_at IS NULL"
    )


def test_image_event_and_preset_revision_keys() -> None:
    events = Base.metadata.tables["image_job_events"]
    assert {"job_id", "sequence"} == {c.name for c in events.primary_key.columns}
    revisions = Base.metadata.tables["image_preset_revisions"]
    assert {"preset_id", "revision"} == {c.name for c in revisions.primary_key.columns}
    assert {
        "defaults",
        "prefix",
        "dependency_ids",
        "entitlement_requirements",
        "created_by_user_id",
    } <= set(revisions.c.keys())


def test_image_schema_migration_has_guarded_reverse() -> None:
    path = Path("apps/coire-api/alembic/versions/0023_image_jobs_presets.py")
    namespace = runpy.run_path(str(path))
    assert namespace["down_revision"] == "0022_stopped_usage_outcome"
    assert "image_preset_revision_immutable" in path.read_text()

    class OccupiedBind:
        def scalar(self, statement: object) -> int:
            return 1

    class OccupiedOp:
        def get_bind(self) -> OccupiedBind:
            return OccupiedBind()

    namespace["downgrade"].__globals__["op"] = OccupiedOp()
    with pytest.raises(RuntimeError, match="image records"):
        namespace["downgrade"]()


@pytest.mark.integration
def test_postgres_image_quota_serializes_independent_admissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first quota row and a one-job cap stay atomic across DB connections."""
    admin_dsn = os.environ.get("COIRE_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("set COIRE_TEST_POSTGRES_DSN for disposable local PostgreSQL")
    parsed = urlparse(admin_dsn)
    if parsed.scheme not in {"postgresql", "postgres"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("quota test requires an explicit local PostgreSQL server")
    database = "coire_image_quota_" + uuid.uuid4().hex[:12]
    test_dsn = urlunparse(parsed._replace(path="/" + database))
    owner_id = uuid.uuid4()

    async def create_database() -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(f"CREATE DATABASE {database}")
        finally:
            await connection.close()

    async def drop_database() -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            await connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
                database,
            )
            await connection.execute(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await connection.close()

    async def exercise() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute(
                "INSERT INTO users (id, email, display_name, role) "
                "VALUES ($1, 'image-quota@example.test', 'Quota', 'user')",
                owner_id,
            )
        finally:
            await connection.close()
        engine = create_async_engine(test_dsn.replace("postgresql:", "postgresql+asyncpg:", 1))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        settings = Settings(  # type: ignore[call-arg]
            _secrets_dir="/nonexistent",
            image_pending_per_owner=1,
            image_pending_global=1,
            image_output_max_bytes=1024,
            image_owner_storage_quota_bytes=4096,
            image_global_storage_quota_bytes=4096,
        )
        monkeypatch.setattr(
            job_capacity,
            "_free_bytes",
            lambda _: settings.image_disk_safety_floor_bytes + 4096,
        )

        async def reserve() -> int:
            async with sessions() as session, session.begin():
                return await job_capacity.reserve_image_job_capacity(session, owner_id, 1, settings)

        try:
            outcomes = await asyncio.gather(reserve(), reserve(), return_exceptions=True)
            assert sorted(type(item).__name__ for item in outcomes) == [
                "ImageQuotaExceeded",
                "int",
            ]
            assert sum(isinstance(item, ImageQuotaExceeded) for item in outcomes) == 1
            async with sessions() as session:
                rows = (
                    await session.scalars(select(ImageQuotaRow).order_by(ImageQuotaRow.scope))
                ).all()
            assert [
                (row.scope, row.pending_jobs, row.held_outputs, row.held_bytes) for row in rows
            ] == [
                ("global", 1, 1, 1024),
                ("owner", 1, 1, 1024),
            ]

            job_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
            now = datetime.now(UTC)
            spec = ImageSpec(
                model_id=uuid.uuid4(),
                prompt="private race test",
                width=64,
                height=64,
                steps=2,
                guidance=Decimal(0),
            )
            async with sessions() as session, session.begin():
                session.add(
                    ImageJobRow(
                        id=job_id,
                        owner_user_id=owner_id,
                        idempotency_key="cancel-publication-race",
                        intent_sha256="a" * 64,
                        submitted_spec=spec.model_dump(mode="json"),
                        resolved_spec=ImageJobSettingsSnapshot(effective_spec=spec).model_dump(
                            mode="json"
                        ),
                        state="transferring",
                        version=1,
                        attempt=1,
                        fence=1,
                        deadline_at=now + timedelta(hours=1),
                        authorization_snapshot={
                            "required_entitlements": [],
                            "explicit": False,
                            "output_hold_bytes": 1024,
                        },
                    )
                )
                await session.flush()
                session.add(
                    ImageJobEventRow(
                        job_id=job_id,
                        sequence=1,
                        event_type="queued",
                        payload={},
                        created_at=now,
                    )
                )

            entered = asyncio.Event()
            release = asyncio.Event()

            async def hold_cancel_after_job_lock(
                session: object, principal: Principal, **kwargs: object
            ) -> uuid.UUID:
                del session, principal, kwargs
                entered.set()
                await release.wait()
                return owner_id

            monkeypatch.setattr(
                cancellation, "authorize_live_image_action", hold_cancel_after_job_lock
            )
            principal = Principal(kind=PrincipalKind.USER, user_id=owner_id, subject=str(owner_id))

            async def cancel() -> None:
                async with sessions() as session:
                    result, terminal = await cancellation.request_image_job_cancel(
                        session, principal, job_id
                    )
                assert result.state == "cancelling" and not terminal

            async def publish() -> None:
                async with sessions() as session, session.begin():
                    await storage.publish_image_batch(session, job_id, settings)

            cancel_task = asyncio.create_task(cancel())
            await asyncio.wait_for(entered.wait(), timeout=2)
            publish_task = asyncio.create_task(publish())
            await asyncio.sleep(0.05)
            assert not publish_task.done(), "publication bypassed the held cancel transaction"
            release.set()
            await cancel_task
            with pytest.raises(ImageConflict, match="publication is not ready"):
                await publish_task
            async with sessions() as session:
                row = await session.get(ImageJobRow, job_id)
            assert row is not None and row.state == "cancelling"
            assert row.cancel_requested_at is not None

            # Retention runs against the real migrated schema and independent transactions.
            retained_job_id = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
            output_ids = [uuid.uuid4() for _ in range(3)]
            async with sessions() as session, session.begin():
                session.add(
                    ImageJobRow(
                        id=retained_job_id,
                        owner_user_id=owner_id,
                        idempotency_key="retention-transaction",
                        intent_sha256="b" * 64,
                        submitted_spec=spec.model_dump(mode="json"),
                        resolved_spec=ImageJobSettingsSnapshot(effective_spec=spec).model_dump(
                            mode="json"
                        ),
                        state="succeeded",
                        deadline_at=now + timedelta(hours=1),
                        authorization_snapshot={},
                        receipt_state="committed",
                        cleanup_state="complete",
                        finished_at=now,
                    )
                )
                await session.flush()
                for index, output_id in enumerate(output_ids):
                    session.add(
                        ImageOutputRow(
                            id=output_id,
                            job_id=retained_job_id,
                            owner_user_id=owner_id,
                            output_index=index,
                            blob_key=output_id.hex,
                            size_bytes=30,
                            file_sha256="c" * 64,
                            pixel_sha256="d" * 64,
                            recipe={},
                            content_tag="unknown",
                            classifier_provenance={},
                            entitlement_snapshot={},
                            state="staged" if index == 2 else "published",
                            created_at=now - timedelta(hours=26),
                            published_at=now - timedelta(hours=25 if index != 1 else 1),
                        )
                    )
                for quota in (await session.scalars(select(ImageQuotaRow))).all():
                    quota.stored_bytes = 90

            @asynccontextmanager
            async def retention_session() -> AsyncIterator[AsyncSession]:
                async with sessions() as session, session.begin():
                    yield session

            monkeypatch.setattr(maintenance, "session_scope", retention_session)
            assert await maintenance.sweep_retained_outputs(settings) == 0
            settings.image_output_retention_hours = 24
            async with sessions() as locked, locked.begin():
                await locked.scalar(
                    select(ImageOutputRow)
                    .where(ImageOutputRow.id == output_ids[0])
                    .with_for_update()
                )
                assert await asyncio.wait_for(maintenance.sweep_retained_outputs(settings), 2) == 0

            original_emit = write_audit

            async def reject_audit(*args: object, **kwargs: object) -> None:
                raise RuntimeError("retention audit failure")

            monkeypatch.setattr(maintenance, "write_audit", reject_audit)
            with pytest.raises(RuntimeError, match="retention audit failure"):
                await maintenance.sweep_retained_outputs(settings)
            async with sessions() as session:
                unchanged = await session.get(ImageOutputRow, output_ids[0])
                assert unchanged is not None and unchanged.deleted_at is None
            monkeypatch.setattr(maintenance, "write_audit", original_emit)
            assert await maintenance.sweep_retained_outputs(settings) == 1
            assert await maintenance.sweep_retained_outputs(settings) == 0
            async with sessions() as session:
                outputs = [await session.get(ImageOutputRow, identity) for identity in output_ids]
                audits = (
                    await session.scalars(
                        select(AuditRow).where(AuditRow.action == "image.output.expire")
                    )
                ).all()
                retained_quota = (await session.scalars(select(ImageQuotaRow))).all()
            assert len(retained_quota) == 2 and all(
                quota.stored_bytes == 90 for quota in retained_quota
            )
            async with sessions() as session:
                drift = await maintenance.reconcile_stored_image_quota(session)
            assert drift.mismatched_rows == 2 and drift.max_abs_drift_bytes == 30
            async with sessions() as session, session.begin():
                for quota in (await session.scalars(select(ImageQuotaRow))).all():
                    quota.stored_bytes = 60
            async with sessions() as session:
                reconciled = await maintenance.reconcile_stored_image_quota(session)
            assert reconciled.mismatched_rows == 0 and reconciled.max_abs_drift_bytes == 0
            input_id = uuid.uuid4()
            async with sessions() as session, session.begin():
                session.add(
                    ImageInputRow(
                        id=input_id,
                        owner_user_id=owner_id,
                        purpose="recipe",
                        original_key=str(input_id),
                        original_bytes=10,
                        original_sha256="e" * 64,
                        state="ready",
                        recipe={},
                        held_bytes=0,
                        active_references=0,
                    )
                )
                for quota in (await session.scalars(select(ImageQuotaRow))).all():
                    quota.stored_bytes = 70
            async with sessions() as session:
                with_input = await maintenance.reconcile_stored_image_quota(session)
            assert with_input.mismatched_rows == 0 and with_input.max_abs_drift_bytes == 0
            async with sessions() as session, session.begin():
                image_input = await session.get(ImageInputRow, input_id)
                assert image_input is not None
                image_input.state = "deleting"
                image_input.deleted_at = now
            async with sessions() as session:
                tombstoned = await maintenance.reconcile_stored_image_quota(session)
            assert tombstoned.mismatched_rows == 0 and tombstoned.max_abs_drift_bytes == 0
            assert all(output is not None and output.purged_at is None for output in outputs)
            assert outputs[0] is not None and outputs[0].deleted_at is not None
            assert all(output is not None and output.deleted_at is None for output in outputs[1:])
            assert len(audits) == 1 and audits[0].actor == "system:image-retention"
        finally:
            await engine.dispose()

    monkeypatch.setenv("POSTGRES_HOST", parsed.hostname or "localhost")
    monkeypatch.setenv("POSTGRES_PORT", str(parsed.port or 5432))
    monkeypatch.setenv("POSTGRES_USER", unquote(parsed.username or ""))
    monkeypatch.setenv("POSTGRES_PASSWORD", unquote(parsed.password or ""))
    monkeypatch.setenv("POSTGRES_DB", database)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    asyncio.run(create_database())
    try:
        command.upgrade(config, "head")
        asyncio.run(exercise())
    finally:
        asyncio.run(drop_database())


@pytest.mark.integration
def test_postgres_image_migration_roundtrip_preserves_text_vlm_and_refuses_live_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_dsn = os.environ.get("COIRE_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("set COIRE_TEST_POSTGRES_DSN for disposable local PostgreSQL")
    parsed = urlparse(admin_dsn)
    if parsed.scheme not in {"postgresql", "postgres"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("migration roundtrip requires an explicit localhost PostgreSQL server")
    database = "coire_image_migration_" + uuid.uuid4().hex[:12]
    dsn = urlunparse(parsed._replace(path="/" + database))
    text_id, vlm_id, image_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def database_command(create: bool) -> None:
        connection = await asyncpg.connect(admin_dsn)
        try:
            if create:
                await connection.execute(f"CREATE DATABASE {database}")
            else:
                await connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
                    database,
                )
                await connection.execute(f"DROP DATABASE IF EXISTS {database}")
        finally:
            await connection.close()

    async def insert_models(image: bool = False) -> None:
        connection = await asyncpg.connect(dsn)
        try:
            for identity, backend, suffix in (
                [(image_id, "mflux", "image")]
                if image
                else [(text_id, "mlx_lm", "text"), (vlm_id, "mlx_vlm", "vlm")]
            ):
                columns = ", kind" if image else ""
                value = ", 'image_model'" if image else ""
                await connection.execute(
                    "INSERT INTO models (id, repo_id, slug, display_name, state, visibility, "
                    "entitlement, tags, placement_policy, precision, weight_bytes, total_bytes, "
                    f"file_count, memory_estimate_bytes, capability_profile, backend, source{columns}) "
                    "VALUES ($1, $2, $3, $4, 'downloading', 'admin_only', '[]', '[]', "
                    f"'single:auto', 'bf16', 1, 1, 1, 1, '{{}}', $5, 'studio'{value})",
                    identity,
                    f"fixture/{suffix}",
                    f"fixture--{suffix}",
                    suffix,
                    backend,
                )
        finally:
            await connection.close()

    async def check_models(*, upgraded: bool, image: bool = False) -> None:
        connection = await asyncpg.connect(dsn)
        try:
            rows = await connection.fetch(
                "SELECT id, backend, repo_id FROM models ORDER BY repo_id"
            )
            expected = [(text_id, "mlx_lm", "fixture/text"), (vlm_id, "mlx_vlm", "fixture/vlm")]
            if image:
                expected.insert(0, (image_id, "mflux", "fixture/image"))
            assert [tuple(row) for row in rows] == expected
            columns = await connection.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'models'"
            )
            assert ("kind" in {row[0] for row in columns}) is upgraded
            assert ("image_capability_profile" in {row[0] for row in columns}) is upgraded
            assert (
                await connection.fetchval("SELECT to_regclass('image_jobs')") is not None
            ) is upgraded
            assert (
                await connection.fetchval("SELECT to_regclass('ix_image_outputs_retention')")
                is not None
            ) is upgraded
        finally:
            await connection.close()

    async def remove_image() -> None:
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute("DELETE FROM models WHERE id = $1", image_id)
        finally:
            await connection.close()

    for name, value in {
        "POSTGRES_HOST": parsed.hostname or "localhost",
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_USER": unquote(parsed.username or ""),
        "POSTGRES_PASSWORD": unquote(parsed.password or ""),
        "POSTGRES_DB": database,
    }.items():
        monkeypatch.setenv(name, value)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    asyncio.run(database_command(True))
    try:
        command.upgrade(config, "0022_stopped_usage_outcome")
        asyncio.run(insert_models())
        asyncio.run(check_models(upgraded=False))
        command.upgrade(config, "head")
        asyncio.run(check_models(upgraded=True))
        asyncio.run(insert_models(image=True))
        with pytest.raises(RuntimeError, match="remove image assets"):
            command.downgrade(config, "0022_stopped_usage_outcome")
        asyncio.run(check_models(upgraded=True, image=True))
        asyncio.run(remove_image())
        command.downgrade(config, "0022_stopped_usage_outcome")
        asyncio.run(check_models(upgraded=False))
        command.upgrade(config, "head")
        asyncio.run(check_models(upgraded=True))
    finally:
        asyncio.run(database_command(False))
