"""Real transaction boundaries for pending recovery transfer; no native effects."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_upgrade_expired_recovery_transfer import transfer_payload

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    AuditRow,
    Base,
    UpgradeExpiredRecoveryRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UserRow,
)
from coire_api.upgrades.expired_recovery import accept_expired_recovery
from coire_api.upgrades.expired_recovery_transfer import prepare_transfer
from coire_api.upgrades.persistence import register_release
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.auth import UserRole
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerTransfer
from coire_core.models.upgrades import UpgradePhase


@pytest.mark.parametrize("retirement_kind", ["acknowledged", "observed"])
async def test_postgres_transfer_audit_failure_concurrency_and_replay(
    training_postgres_url, tmp_path, monkeypatch, retirement_kind
):
    transfer = UpgradeExpiredRecoveryOwnerTransfer.model_validate(transfer_payload(tmp_path))
    intent = transfer.intent
    plan = intent.original_plan
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    actor = Principal(kind=PrincipalKind.ADMIN, user_id=intent.accepted_by, role=UserRole.ADMIN)
    preparer = Principal(
        kind=PrincipalKind.ADMIN, user_id=plan.operation.user_id, role=UserRole.ADMIN
    )
    schema = "recovery_transfer_" + uuid4().hex
    root = create_async_engine(training_postgres_url)
    engine = create_async_engine(
        training_postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with root.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            for owner in {intent.accepted_by, operation.user_id}:
                session.add(
                    UserRow(
                        id=owner,
                        email=f"{owner}@example.test",
                        display_name="Fixture administrator",
                        role=UserRole.ADMIN,
                    )
                )
        async with sessions.begin() as session:
            await register_release(session, preparer, plan.source)
            await register_release(session, preparer, plan.target)
            session.add(
                UpgradeOperationRow(
                    id=operation.job_id,
                    user_id=operation.user_id,
                    source_release_id=plan.source.release_id,
                    target_release_id=plan.target.release_id,
                    idempotency_key=operation.idempotency_key,
                    request_sha256=operation.request_sha256,
                    generation=operation.generation,
                    active_slot=1,
                    phase="recovery_required",
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
        # Capture synthetic fixture epoch only after setup, before first acceptance.
        now = datetime.now(UTC)
        intent = intent.model_copy(
            update={"accepted_at": now, "deadline_at": now + timedelta(seconds=300)}
        )
        async with sessions.begin() as session:
            await accept_expired_recovery(session, actor, intent)
        issued = datetime.now(UTC)
        transfer = transfer.model_copy(
            update={
                "intent": intent,
                "issued_at": issued,
                "expires_at": issued + timedelta(seconds=30),
                "legacy_guard": transfer.legacy_guard.model_copy(update={"observed_at": issued}),
            }
        )

        async def reserve(value):
            async with sessions.begin() as session:
                return await prepare_transfer(session, actor, value)

        from coire_api.upgrades import expired_recovery_transfer as service

        async def unavailable_audit(*args, **kwargs):
            raise RuntimeError("fixture transfer audit failure")

        with monkeypatch.context() as patch:
            patch.setattr(service, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError, match="fixture transfer audit failure"):
                await reserve(transfer)
        async with sessions.begin() as session:
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            assert row.phase == "accepted" and row.transfer_payload is None
        results = await asyncio.gather(reserve(transfer), reserve(transfer))
        assert sum(created for _, created in results) == 1
        assert all(value == transfer for value, _ in results)
        value, created = await reserve(transfer)  # Lost response/restart-style exact replay.
        assert value == transfer and not created
        with pytest.raises(UpgradeConflict):
            await reserve(transfer.model_copy(update={"transfer_id": uuid4()}))
        from coire_api.upgrades import expired_recovery_consumption as consumption

        async def consume():
            async with sessions.begin() as session:
                return await consumption.consume_transfer(session, actor, transfer)

        # Synthetic ledger rows are not native qualification evidence.
        with pytest.raises(UpgradeConflict):
            await consume()  # Unacknowledged drain must refuse consumption.
        async with sessions.begin() as session:
            fences = (await session.scalars(select(UpgradeMaintenanceRow))).all()
            active = plan.maintenance.model_copy(
                update={
                    "state": "active",
                    "acknowledged_generations": dict.fromkeys(
                        plan.maintenance.targets, operation.generation
                    ),
                }
            )
            for fence in fences:
                fence.payload = active.model_dump(mode="json")
        with monkeypatch.context() as patch:
            patch.setattr(consumption, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError, match="fixture transfer audit failure"):
                await consume()
        async with sessions.begin() as session:
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            assert row.phase == "transferring" and row.consumption_payload is None
        from coire_api.upgrades.expired_recovery_delivery import read_consumption_state

        async with sessions.begin() as session:
            pending = await read_consumption_state(
                session,
                intent.recovery_id,
                intent.sha256(),
                transfer.successor,
                plan.operation.node,
            )
            assert pending.consumption is None
        consumed = await asyncio.gather(consume(), consume())
        assert sum(created for _, created in consumed) == 1
        assert consumed[0][0] == consumed[1][0]
        replay, created = await consume()
        assert replay == consumed[0][0] and not created
        async with sessions.begin() as session:
            verified = await consumption.verify_live_consumption(
                session, intent.recovery_id, replay.sha256(), replay.consumer, plan.operation.node
            )
            assert verified == replay
        from coire_api.upgrades import expired_recovery_owner as ownership
        from coire_core.models.upgrade_recovery import (
            UpgradeExpiredRecoveryLegacyService,
            UpgradeExpiredRecoveryLegacyStopIntent,
            UpgradeExpiredRecoveryLegacyStopReceipt,
            UpgradeExpiredRecoveryLegacyStopReconciliation,
            UpgradeExpiredRecoveryOwnerClaim,
        )
        from coire_core.models.upgrades import NativeNodeProcess

        async with sessions.begin() as session:
            assert (
                await ownership.read_live_support_context(
                    session, intent.recovery_id, plan.operation.node
                )
                == intent
            )
        with pytest.raises(UpgradeForbidden):
            async with sessions.begin() as session:
                await ownership.read_live_support_context(
                    session, intent.recovery_id, "coire-edge-b"
                )
        async with sessions.begin() as session:
            delivered = await read_consumption_state(
                session, intent.recovery_id, intent.sha256(), replay.consumer, plan.operation.node
            )
            assert delivered.consumption == replay
        with pytest.raises(UpgradeForbidden):
            async with sessions.begin() as session:
                await read_consumption_state(
                    session,
                    intent.recovery_id,
                    intent.sha256(),
                    replay.consumer.model_copy(update={"pid": replay.consumer.pid + 1}),
                    plan.operation.node,
                )
        claimed = datetime.now(UTC)
        source = NativeNodeProcess(
            selected=transfer.legacy_guard.selected,
            process=transfer.legacy_guard.process.model_copy(update={"pid": 50000}),
            recorded_at=claimed,
        )
        managed = UpgradeExpiredRecoveryLegacyService(
            target="system/com.coire.upgrade-guard",
            owner_uid=501,
            abandon_process_group=True,
            process=transfer.legacy_guard.process,
            observation_sha256="a" * 64,
            observed_at=claimed,
        )
        effect = UpgradeExpiredRecoveryLegacyStopIntent(
            recovery_id=intent.recovery_id,
            consumption_sha256=replay.sha256(),
            legacy_guard=transfer.legacy_guard,
            successor=replay.consumer,
            started_at=claimed,
            service=managed,
            definition_sha256="b" * 64,
            preserved_source=source,
        )
        stop = UpgradeExpiredRecoveryLegacyStopReceipt(
            effect=effect, preserved_source=source, stopped_at=claimed
        )
        support = intent.support_release.manifest
        runtime = transfer.legacy_guard.bootstrap_runtime.model_copy(
            update={
                "environment_id": support.environment_id,
                "environment_sha256": support.environment_sha256,
            }
        )
        claim = UpgradeExpiredRecoveryOwnerClaim(
            consumption=replay,
            legacy_stop=stop
            if retirement_kind == "acknowledged"
            else UpgradeExpiredRecoveryLegacyStopReconciliation(
                consumption=replay,
                effect=effect,
                preserved_source=source,
                observed_at=claimed,
                service_unloaded=True,
                legacy_identity_retired=True,
            ),
            process=replay.consumer,
            runtime=runtime,
            kernel_boot_identity="synthetic-kernel",
            claimed_at=claimed,
        )

        async with sessions.begin() as session:
            state = await ownership.probe_owner_acceptance(
                session, intent.recovery_id, claim.sha256(), claim.process, plan.operation.node
            )
            assert state.owner is None and state.owner_sha256 == claim.sha256()
        from types import SimpleNamespace

        with monkeypatch.context() as patch:
            patch.setattr(
                consumption,
                "datetime",
                SimpleNamespace(now=lambda zone: transfer.expires_at + timedelta(seconds=1)),
            )
            with pytest.raises(UpgradeUnavailable):
                async with sessions.begin() as session:
                    await ownership.probe_owner_acceptance(
                        session,
                        intent.recovery_id,
                        claim.sha256(),
                        claim.process,
                        plan.operation.node,
                    )

        async def retain_owner(value=claim):
            async with sessions.begin() as session:
                return await ownership.accept_owner_claim(session, value, plan.operation.node)

        with monkeypatch.context() as patch:
            patch.setattr(ownership, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError, match="fixture transfer audit failure"):
                await retain_owner()
        async with sessions.begin() as session:
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            assert row.phase == "transfer_consumed" and row.owner_claim_payload is None
        owners = await asyncio.gather(retain_owner(), retain_owner())
        assert sum(created for _, created in owners) == 1
        assert await retain_owner() == (claim, False)
        async with sessions.begin() as session:
            state = await ownership.probe_owner_acceptance(
                session, intent.recovery_id, claim.sha256(), claim.process, plan.operation.node
            )
            assert state.owner == claim
        with pytest.raises(UpgradeConflict):
            await retain_owner(claim.model_copy(update={"kernel_boot_identity": "foreign-kernel"}))
        async with sessions.begin() as session:
            assert (
                await ownership.verify_live_owner(
                    session, intent.recovery_id, claim.sha256(), claim.process, plan.operation.node
                )
                == claim
            )
        with monkeypatch.context() as patch:
            patch.setattr(
                ownership,
                "datetime",
                SimpleNamespace(now=lambda zone: claimed + timedelta(hours=1)),
            )
            assert await retain_owner() == (claim, False)  # Inspection cannot renew epoch.
            with pytest.raises(UpgradeUnavailable):
                async with sessions.begin() as session:
                    await ownership.verify_live_owner(
                        session,
                        intent.recovery_id,
                        claim.sha256(),
                        claim.process,
                        plan.operation.node,
                    )
        from coire_api.upgrades import expired_recovery_succession as succession
        from coire_api.upgrades import (
            expired_recovery_succession_orchestration as succession_orchestration,
        )
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessionObservation

        observation = UpgradeExpiredRecoverySuccessionObservation(
            succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
            previous_owner=claim,
            successor=claim.process.model_copy(update={"pid": claim.process.pid + 1}),
            kernel_boot_identity=claim.kernel_boot_identity,
            preserved_source=claim.legacy_stop.preserved_source,
            observed_at=claimed,
            previous_owner_retired=True,
        )

        async def retain_succession(proof=observation):
            async with sessions.begin() as session:
                return await succession.accept_succession(
                    session, actor, proof, plan.operation.node
                )

        with monkeypatch.context() as patch:
            patch.setattr(succession, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError):
                await retain_succession()

        from coire_api.upgrades.expired_recovery_observation_relay import SuccessionObservationRelay

        relay = SuccessionObservationRelay()
        with pytest.raises(UpgradeUnavailable):
            await succession_orchestration.accept_with_trusted_relay(
                sessions, actor, claim, observation.succession_id, relay
            )
        relay.publish(observation)

        async def orchestrated_succession():
            return await succession_orchestration.accept_with_trusted_relay(
                sessions, actor, claim, observation.succession_id, relay
            )

        from coire_api.upgrades.expired_recovery_succession_delivery import read_succession_delivery
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessionDeliveryRead

        delivery_read = UpgradeExpiredRecoverySuccessionDeliveryRead(
            succession_id=observation.succession_id,
            predecessor_sha256=claim.sha256(),
            successor=observation.successor,
        )
        async with sessions.begin() as session:
            pending_delivery = await read_succession_delivery(
                session, intent.recovery_id, delivery_read, transfer.legacy_guard.node
            )
            assert pending_delivery.grant is None
        results = await asyncio.gather(orchestrated_succession(), retain_succession())
        async with sessions.begin() as session:
            delivered = await read_succession_delivery(
                session, intent.recovery_id, delivery_read, transfer.legacy_guard.node
            )
            assert delivered.grant == results[0][0]
        assert sorted(inserted for _, inserted in results) == [False, True]
        grant = results[0][0]
        assert await retain_succession() == (grant, False)
        assert grant.expires_at <= intent.deadline_at

        async def gate_succession(process=observation.successor, node=plan.operation.node):
            async with sessions.begin() as session:
                return await succession.verify_live_succession_grant(
                    session, observation.succession_id, grant.sha256(), process, node
                )

        assert await gate_succession() == grant
        from coire_api.upgrades import expired_recovery_successor as successor_api
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySuccessorClaim

        successor_claim = UpgradeExpiredRecoverySuccessorClaim(
            grant=grant,
            process=observation.successor,
            runtime=claim.runtime,
            kernel_boot_identity=claim.kernel_boot_identity,
            preserved_source=claim.legacy_stop.preserved_source,
            claimed_at=datetime.now(UTC),
        )

        async def retain_successor():
            async with sessions.begin() as session:
                return await successor_api.accept_successor_claim(
                    session, successor_claim, plan.operation.node
                )

        async def probe_successor():
            async with sessions.begin() as session:
                return await successor_api.probe_successor_acceptance(
                    session,
                    observation.succession_id,
                    successor_claim.sha256(),
                    successor_claim.process,
                    plan.operation.node,
                    grant_sha256=grant.sha256(),
                )

        with pytest.raises(UpgradeForbidden):
            async with sessions.begin() as session:
                await successor_api.probe_successor_acceptance(
                    session,
                    observation.succession_id,
                    successor_claim.sha256(),
                    successor_claim.process,
                    plan.operation.node,
                    grant_sha256="f" * 64,
                )
        assert (await probe_successor()).owner is None
        with monkeypatch.context() as patch:
            patch.setattr(successor_api, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError):
                await retain_successor()
        successor_results = await asyncio.gather(retain_successor(), retain_successor())
        assert sorted(inserted for _, inserted in successor_results) == [False, True]
        assert await retain_successor() == (successor_claim, False)
        assert (await probe_successor()).owner == successor_claim

        async def gate_successor():
            async with sessions.begin() as session:
                return await successor_api.verify_live_successor(
                    session,
                    observation.succession_id,
                    successor_claim.sha256(),
                    successor_claim.process,
                    plan.operation.node,
                )

        assert await gate_successor() == successor_claim
        with monkeypatch.context() as patch:
            patch.setattr(
                successor_api, "datetime", SimpleNamespace(now=lambda zone: grant.expires_at)
            )
            assert await gate_successor() == successor_claim
            assert (await probe_successor()).owner == successor_claim
            patch.setattr(
                successor_api, "datetime", SimpleNamespace(now=lambda zone: intent.deadline_at)
            )
            with pytest.raises(UpgradeUnavailable):
                await gate_successor()
            assert await retain_successor() == (successor_claim, False)
        with pytest.raises(UpgradeForbidden):
            await gate_succession(claim.process)
        with pytest.raises(UpgradeForbidden):
            await gate_succession(node="foreign-node")
        with monkeypatch.context() as patch:
            patch.setattr(
                succession, "datetime", SimpleNamespace(now=lambda zone: grant.expires_at)
            )
            with pytest.raises(UpgradeUnavailable):
                await gate_succession()
            assert await retain_succession() == (grant, False)
        async with sessions.begin() as session:
            succession_audits = (
                await session.scalars(
                    select(AuditRow).where(
                        AuditRow.action == "upgrades.expired_recovery.succession.accept"
                    )
                )
            ).all()
            assert len(succession_audits) == 1
        with pytest.raises(UpgradeConflict):
            await retain_succession(
                observation.model_copy(update={"succession_id": "01ARZ3NDEKTSV4RRFFQ69G5FAY"})
            )
        with pytest.raises(UpgradeUnavailable):
            async with sessions.begin() as session:
                await ownership.verify_live_owner(
                    session, intent.recovery_id, claim.sha256(), claim.process, plan.operation.node
                )
        async with sessions.begin() as session:
            user = await session.get(UserRow, intent.accepted_by)
            user.role = UserRole.USER
        with pytest.raises(UpgradeForbidden):
            async with sessions.begin() as session:
                await consumption.verify_live_consumption(
                    session,
                    intent.recovery_id,
                    replay.sha256(),
                    replay.consumer,
                    plan.operation.node,
                )
        with pytest.raises(UpgradeForbidden):
            async with sessions.begin() as session:
                await ownership.verify_live_owner(
                    session, intent.recovery_id, claim.sha256(), claim.process, plan.operation.node
                )
        with pytest.raises(UpgradeForbidden):
            await retain_owner()
        with pytest.raises(UpgradeForbidden):
            await gate_succession()
        with pytest.raises(UpgradeForbidden):
            await gate_successor()
        with pytest.raises(UpgradeForbidden):
            await retain_successor()
        with pytest.raises(UpgradeForbidden):
            await retain_succession()
        with pytest.raises(UpgradeForbidden):
            async with sessions.begin() as session:
                await ownership.read_live_support_context(
                    session, intent.recovery_id, plan.operation.node
                )
        with pytest.raises(UpgradeForbidden):
            async with sessions.begin() as session:
                await ownership.probe_owner_acceptance(
                    session, intent.recovery_id, claim.sha256(), claim.process, plan.operation.node
                )
        async with sessions.begin() as session:
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            assert row.phase == "recovering"
            assert row.owner_claim_payload == claim.model_dump(mode="json")
            owner_audits = (
                await session.scalars(
                    select(AuditRow).where(
                        AuditRow.action == "upgrades.expired_recovery.owner.accept"
                    )
                )
            ).all()
            assert len(owner_audits) == 1
            assert row.consumption_payload == replay.model_dump(mode="json")
            assert row.transfer_payload == transfer.model_dump(mode="json")
            audits = (
                await session.scalars(
                    select(AuditRow).where(
                        AuditRow.action == "upgrades.expired_recovery.transfer.prepare"
                    )
                )
            ).all()
            assert len(audits) == 1
            consumptions = (
                await session.scalars(
                    select(AuditRow).where(
                        AuditRow.action == "upgrades.expired_recovery.transfer.consume"
                    )
                )
            ).all()
            assert len(consumptions) == 1
            original = await session.get(UpgradeOperationRow, operation.job_id)
            assert original.payload == operation.model_dump(mode="json")
            assert original.execution_plan == plan.model_dump(mode="json")
            fences = (await session.scalars(select(UpgradeMaintenanceRow))).all()
            assert all(not fence.released for fence in fences)

        # A different live administrator can revoke even a demoted initiator's
        # accepted successor. Cancellation keeps every historical preimage/hold.
        from coire_api.upgrades import expired_recovery_cancel as cancellation_service
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryCancelRequest

        cancel_request = UpgradeExpiredRecoveryCancelRequest(intent_sha256=intent.sha256())

        async def cancel(value=cancel_request, administrator=preparer):
            async with sessions.begin() as session:
                return await cancellation_service.cancel_recovery(
                    session, administrator, intent.recovery_id, value
                )

        with pytest.raises(UpgradeForbidden):
            await cancel(administrator=actor)
        with pytest.raises(UpgradeConflict):
            await cancel(UpgradeExpiredRecoveryCancelRequest(intent_sha256="f" * 64))
        with monkeypatch.context() as patch:
            patch.setattr(cancellation_service, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError):
                await cancel()
        async with sessions.begin() as session:
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            assert row.phase == "recovering" and row.cancellation_payload is None
        cancellations = await asyncio.gather(cancel(), cancel())
        assert sorted(created for _, created in cancellations) == [False, True]
        proof = cancellations[0][0]
        assert cancellations[1][0] == proof
        assert await cancel() == (proof, False)
        # Revocation, not just demotion, must block every live effect gate.
        async with sessions.begin() as session:
            user = await session.get(UserRow, intent.accepted_by)
            user.role = UserRole.ADMIN
        for gate in (gate_successor, gate_succession):
            with pytest.raises(UpgradeUnavailable):
                await gate()
        with pytest.raises(UpgradeUnavailable):
            async with sessions.begin() as session:
                await ownership.read_live_support_context(
                    session, intent.recovery_id, plan.operation.node
                )
        with pytest.raises(UpgradeUnavailable):
            async with sessions.begin() as session:
                await consumption.verify_live_consumption(
                    session,
                    intent.recovery_id,
                    replay.sha256(),
                    replay.consumer,
                    plan.operation.node,
                )
        with monkeypatch.context() as patch:
            patch.setattr(
                cancellation_service,
                "datetime",
                SimpleNamespace(now=lambda zone: intent.deadline_at + timedelta(hours=1)),
            )
            assert await cancel() == (proof, False)
        async with sessions.begin() as session:
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            assert row.phase == "recovery_required" and row.active_slot == 1
            assert row.cancellation_payload == proof.model_dump(mode="json")
            assert row.intent_payload == intent.model_dump(mode="json")
            assert row.owner_claim_payload == claim.model_dump(mode="json")
            assert row.consumption_payload == replay.model_dump(mode="json")
            assert row.transfer_payload == transfer.model_dump(mode="json")
            original = await session.get(UpgradeOperationRow, operation.job_id)
            assert original.payload == operation.model_dump(mode="json")
            assert original.execution_plan == plan.model_dump(mode="json")
            fences = (await session.scalars(select(UpgradeMaintenanceRow))).all()
            assert all(not fence.released for fence in fences)
            assert (
                len(
                    (
                        await session.scalars(
                            select(AuditRow).where(
                                AuditRow.action == "upgrades.expired_recovery.cancel"
                            )
                        )
                    ).all()
                )
                == 1
            )
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await root.dispose()
