"""Failure relay is scoped and cannot deliver success or a new execution budget."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from test_upgrade_guard import inputs

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_native_progress import NativeRecoveryFailureReplay
from coire_core.models.upgrades import (
    NativeRecoveryIntent,
    UpgradePhase,
    UpgradeReceipt,
    UpgradeReceiptBatch,
)
from coire_core.settings import Settings
from coire_node.upgrade_guard import NativeGuardTransport


def failure():
    plan, source, candidate, process = inputs()
    now = datetime.now(UTC)
    native = NativeRecoveryIntent(
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        source=source,
        candidate=candidate,
        source_process=process,
        phase="recovery_required",
        updated_at=now,
        activation_grant_id=uuid4(),
        activation_started_at=now - timedelta(seconds=500),
        readiness_deadline_at=now - timedelta(seconds=380),
        recovery_started_at=now - timedelta(seconds=380),
        recovery_deadline_at=now - timedelta(seconds=80),
    )
    receipt = UpgradeReceipt(
        job_id=native.job_id,
        generation=native.generation,
        sequence=1,
        effect_id="native-recovery-required",
        phase=UpgradePhase.RECOVERY_REQUIRED,
        occurred_at=now,
        outcome="unknown",
        reason="native_recovery_deadline",
        restoration="pending",
    )
    return plan, NativeRecoveryFailureReplay(
        native=native, batch=UpgradeReceiptBatch(receipts=(receipt,))
    )


@pytest.mark.parametrize("fault", [None, "node", "plan", "runtime", "success"])
async def test_failure_reconciliation_scopes_node_and_immutable_authority(monkeypatch, fault):
    from coire_api.upgrades import native_failure_replay as relay

    plan, body = failure()
    if fault == "plan":
        body = body.model_copy(
            update={"native": body.native.model_copy(update={"plan_sha256": "f" * 64})}
        )
    if fault == "runtime":
        body = body.model_copy(
            update={
                "native": body.native.model_copy(
                    update={
                        "candidate": body.native.candidate.model_copy(
                            update={"environment_sha256": "f" * 64}
                        )
                    }
                )
            }
        )
    if fault == "success":
        body = body.model_copy(
            update={
                "batch": UpgradeReceiptBatch(
                    receipts=(
                        body.batch.receipts[0].model_copy(update={"phase": UpgradePhase.SUCCEEDED}),
                    )
                )
            }
        )
    row = SimpleNamespace(
        execution_plan=plan.model_dump(mode="json"), payload=plan.operation.model_dump(mode="json")
    )
    monkeypatch.setattr(relay, "operation_lock", AsyncMock())
    monkeypatch.setattr(relay, "get_operation", AsyncMock(return_value=row))
    ingest = AsyncMock()
    monkeypatch.setattr(relay, "ingest_receipt", ingest)
    if fault:
        with pytest.raises((UpgradeConflict, ValueError)):
            await relay.reconcile_native_failure(
                Mock(), body, "coire-edge-b" if fault == "node" else "coire-edge-a"
            )
        ingest.assert_not_awaited()
    else:
        ack = await relay.reconcile_native_failure(Mock(), body, "coire-edge-a")
        assert ack.job_id == plan.operation.job_id
        ingest.assert_awaited_once()


@pytest.mark.parametrize("fault", [None, "lost", "foreign"])
def test_guard_replays_only_retained_expired_failure_and_pins_ack(fault):
    _, body = failure()
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["x-coire-node"] == "coire-edge-a"
        assert request.url.path.endswith("/native-failure-receipts")
        assert NativeRecoveryFailureReplay.model_validate_json(request.content) == body
        if fault == "lost":
            raise httpx.ReadError("lost response", request=request)
        return httpx.Response(
            200,
            json={
                "job_id": body.native.job_id,
                "generation": body.native.generation + (fault == "foreign"),
                "sequence": 1,
            },
        )

    receipts = Mock()
    receipts.latest.return_value = body.batch.receipts[-1]
    receipts.acknowledged_sequence.return_value = 0
    receipts.page.return_value = body.batch.receipts

    def acknowledge(ack):
        if ack.generation != body.native.generation:
            raise UpgradeConflict("foreign acknowledgement")

    receipts.acknowledge.side_effect = acknowledge
    journal = Mock()
    journal.current.return_value = body.native
    journal.receipts = receipts
    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        transport = NativeGuardTransport(
            Settings(
                node_name="coire-edge-a",
                node_control_host="127.0.0.1",
                node_token="fixture-token",
                upgrades_enabled=True,
                legacy_network_mode=False,
            ),
            client,
        )
        if fault:
            with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                transport.relay_expired_failure(journal)
        else:
            transport.relay_expired_failure(journal)
    assert len(calls) == 1


@pytest.mark.parametrize("audit_lost", [False, True])
async def test_actual_postgres_failure_replay_preserves_fences_and_audit_atomicity(
    training_postgres_url, monkeypatch, audit_lost
):
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from coire_api.db import (
        Base,
        UpgradeMaintenanceRow,
        UpgradeOperationRow,
        UpgradeReceiptRow,
        UpgradeReleaseRow,
        UserRow,
    )
    from coire_api.upgrades import persistence
    from coire_api.upgrades.native_failure_replay import reconcile_native_failure
    from coire_core.models.auth import UserRole

    plan, body = failure()
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.ACTIVATING})
    schema = "native_failure_" + uuid4().hex
    admin = create_async_engine(training_postgres_url)
    async with admin.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=operation.user_id,
                    email=str(operation.user_id) + "@isolated.test",
                    display_name="Native failure",
                    role=UserRole.ADMIN,
                )
            )
            await session.flush()
            for release in (plan.source, plan.target):
                session.add(
                    UpgradeReleaseRow(
                        id=release.release_id,
                        manifest_sha256=release.manifest_sha256,
                        prepared_artifact_id=release.prepared_artifact_id,
                        prepared_by=release.prepared_by,
                        payload=release.model_dump(mode="json"),
                    )
                )
            await session.flush()
            session.add(
                UpgradeOperationRow(
                    id=operation.job_id,
                    user_id=operation.user_id,
                    source_release_id=operation.source_release_id,
                    target_release_id=operation.target_release_id,
                    idempotency_key=operation.idempotency_key,
                    request_sha256="a" * 64,
                    generation=operation.generation,
                    active_slot=1,
                    phase=operation.phase.value,
                    payload=operation.model_dump(mode="json"),
                    execution_plan=plan.model_dump(mode="json"),
                )
            )
            await session.flush()
            for target in plan.maintenance.targets:
                session.add(
                    UpgradeMaintenanceRow(
                        target=target,
                        job_id=operation.job_id,
                        generation=operation.generation,
                        released=False,
                        payload=plan.maintenance.model_dump(mode="json"),
                    )
                )
        if audit_lost:
            with monkeypatch.context() as patch:
                patch.setattr(
                    persistence,
                    "write_principal_audit",
                    AsyncMock(side_effect=RuntimeError("lost audit")),
                )
                with pytest.raises(RuntimeError, match="lost audit"):
                    async with sessions.begin() as session:
                        await reconcile_native_failure(session, body, "coire-edge-a")
            async with sessions.begin() as session:
                assert (
                    await session.get(
                        UpgradeReceiptRow, (operation.job_id, operation.generation, 1)
                    )
                    is None
                )
                row = await session.get(UpgradeOperationRow, operation.job_id)
                assert row.phase == "activating" and row.active_slot == 1
        async with sessions.begin() as session:
            ack = await reconcile_native_failure(session, body, "coire-edge-a")
        async with sessions.begin() as session:
            assert await reconcile_native_failure(session, body, "coire-edge-a") == ack
            row = await session.get(UpgradeOperationRow, operation.job_id)
            assert row.phase == "recovery_required" and row.active_slot == 1
            assert row.payload["finished_at"] is None
            for target in plan.maintenance.targets:
                assert not (await session.get(UpgradeMaintenanceRow, target)).released
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
