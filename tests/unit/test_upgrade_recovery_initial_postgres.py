"""Trusted initial observation plus actual audited PostgreSQL acceptance/replay."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_upgrade_expired_recovery_capture import retained_native_fixture

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    AuditRow,
    Base,
    ModelRow,
    ModelVariantRow,
    NodeRow,
    UpgradeExpiredRecoveryRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UserRow,
    VariantCopyRow,
)
from coire_api.upgrades import expired_recovery
from coire_api.upgrades.expired_recovery_data import capture_data_snapshot, verify_data_preserved
from coire_api.upgrades.expired_recovery_initial_relay import (
    InitialRecoveryObservationRelay,
    verify_observation_context,
)
from coire_api.upgrades.expired_recovery_orchestration import accept_with_observation
from coire_api.upgrades.expired_recovery_status import read_recovery_status
from coire_api.upgrades.persistence import register_release
from coire_core.errors import UpgradeForbidden
from coire_core.models.acquisition import VariantState
from coire_core.models.auth import UserRole
from coire_core.models.node import NodeRole
from coire_core.models.registry import CopyRole, ModelState
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryDataSnapshot,
    UpgradeExpiredRecoveryNativeDataSnapshot,
    UpgradeExpiredRecoveryObservation,
    UpgradeRecoveryRetiredSupervisor,
)
from coire_core.models.upgrades import NativeUpgradeClaim, UpgradePhase


@pytest.mark.parametrize(
    "mode",
    [
        "preserve",
        "cold",
        "cold_resolved",
        "unstarted_closed",
        "unstarted_cancelled",
        "pre_projection_closed",
        "cold_continued",
        "archived_continued",
        "post_handback_continued",
        "partial_handback_continued",
        "unstarted_continued",
        "inherited_continued",
    ],
)
async def test_initial_relay_atomic_acceptance_concurrency_replay_and_authority(
    training_postgres_url, tmp_path, monkeypatch, mode
):
    unstarted_continued = mode == "unstarted_continued"
    inherited_continued = mode == "inherited_continued"
    partial_handback_continued = mode == "partial_handback_continued"
    post_handback_continued = (
        partial_handback_continued
        or mode == "post_handback_continued"
        or unstarted_continued
        or inherited_continued
    )
    archived_continued = mode == "archived_continued" or post_handback_continued
    continued = mode == "cold_continued" or archived_continued
    resolve = mode == "cold_resolved"
    pre_projection = mode == "pre_projection_closed"
    cancelled_unstarted = mode == "unstarted_cancelled" or pre_projection
    unstarted = mode in {"unstarted_closed", "unstarted_cancelled", "pre_projection_closed"}
    mode = "cold" if resolve or unstarted or continued else mode
    plan, native, scope, request, _ = retained_native_fixture(tmp_path)
    if mode == "cold":
        native = native.model_copy(
            update={
                "activation_started_at": native.recovery_started_at,
                "activation_grant_id": uuid4(),
                "readiness_deadline_at": native.recovery_started_at + timedelta(seconds=120),
            }
        )
    request = request.model_copy(update={"recovery_mode": mode})
    if continued:
        request = request.model_copy(update={"supervision": "fixture"})
    operation = plan.operation.model_copy(update={"phase": UpgradePhase.RECOVERY_REQUIRED})
    actor = Principal(kind=PrincipalKind.ADMIN, user_id=operation.user_id, role=UserRole.ADMIN)
    schema = "initial_recovery_" + uuid4().hex
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
            await conn.execute(text("CREATE SEQUENCE upgrade_generation_seq"))
            await conn.execute(
                text("SELECT setval('upgrade_generation_seq', :generation)"),
                {"generation": operation.generation},
            )
        async with sessions.begin() as session:
            session.add(
                UserRow(
                    id=operation.user_id,
                    email=f"{operation.user_id}@test.invalid",
                    display_name="Fixture",
                    role=UserRole.ADMIN,
                )
            )
        async with sessions.begin() as session:
            await register_release(session, actor, plan.source)
            await register_release(session, actor, plan.target)
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
        now = datetime.now(UTC)
        proof = UpgradeExpiredRecoveryObservation(
            request=request,
            plan=plan,
            native=native,
            claim=NativeUpgradeClaim(
                job_id=native.job_id,
                generation=native.generation,
                node=native.source.node,
                plan_sha256=native.plan_sha256,
                updated_at=now,
            ),
            scopes=(scope,),
            native_data=UpgradeExpiredRecoveryNativeDataSnapshot(
                original_job_id=native.job_id,
                original_generation=native.generation,
                source_sha256=plan.source.manifest_sha256,
                node=native.source.node,
                root_sha256s=dict.fromkeys(("datasets", "artifacts", "components"), "e" * 64),
                file_counts=dict.fromkeys(("datasets", "artifacts", "components"), 0),
                total_bytes=0,
                observed_at=now,
            ),
            support=plan.target,
            observer_runtime=native.candidate,
            observed_at=now,
        )
        async with sessions.begin() as session:
            await verify_observation_context(session, proof, native.source.node)
        relay = InitialRecoveryObservationRelay()
        relay.publish(proof)
        observations = []

        async def observe(selector, support):
            observations.append(selector.sha256())
            return relay.read(selector, support)

        async def accept():
            return await accept_with_observation(sessions, actor, request, observe)

        async def unavailable_audit(*args, **kwargs):
            raise RuntimeError("fixture audit failure")

        with monkeypatch.context() as patch:
            patch.setattr(expired_recovery, "write_principal_audit", unavailable_audit)
            with pytest.raises(RuntimeError, match="fixture audit failure"):
                await accept()
        async with sessions.begin() as session:
            assert (await session.scalars(select(UpgradeExpiredRecoveryRow))).all() == []
        results = await asyncio.gather(accept(), accept())
        assert sorted(created for _, created in results) == [False, True]
        intent = results[0][0]
        assert results[1][0] == intent
        async with sessions.begin() as session:
            status = await read_recovery_status(session, actor, intent.recovery_id)
            assert status.intent == intent and status.phase == "accepted" and status.active
            assert status.owner_sha256 is None and status.cancellation is None
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            baseline = UpgradeExpiredRecoveryDataSnapshot.model_validate(row.data_baseline_payload)
            assert baseline.row_counts["users"] == 1
            after = await capture_data_snapshot(session, intent)
            verify_data_preserved(baseline, after, intent)
            user = await session.get(UserRow, operation.user_id)
            user.display_name = "Changed fixture"
            await session.flush()
            changed = await capture_data_snapshot(session, intent)
            from coire_core.errors import UpgradeUnavailable

            with pytest.raises(UpgradeUnavailable, match="preserved original user data"):
                verify_data_preserved(baseline, changed, intent)
            user.display_name = "Fixture"
            await session.flush()
        count = len(observations)
        assert await accept() == (intent, False)
        assert len(observations) == count  # Lost-response replay never resamples/recaptures.
        assert intent.original_recovery_deadline_at == native.recovery_deadline_at
        if unstarted:
            await exercise_unstarted_closure(
                sessions,
                actor,
                intent,
                proof,
                native,
                operation,
                monkeypatch,
                cancelled=cancelled_unstarted,
                pre_projection=pre_projection,
            )
            return
        if mode == "cold":
            from types import SimpleNamespace

            from coire_api.upgrades import expired_recovery_cold as cold
            from coire_api.upgrades.expired_recovery_cancel import cancel_recovery
            from coire_core.errors import UpgradeUnavailable
            from coire_core.models.upgrade_recovery import (
                UpgradeExpiredRecoveryCancelRequest,
                UpgradeExpiredRecoveryColdClaim,
            )
            from coire_core.models.upgrades import (
                NativeGuardStatus,
                NativeNodeProcess,
                NodeAgentProcessIdentity,
            )

            async with sessions.begin() as session:
                active = plan.maintenance.model_copy(
                    update={
                        "state": "active",
                        "acknowledged_generations": dict.fromkeys(
                            plan.maintenance.targets, operation.generation
                        ),
                    }
                )
                for fence in (await session.scalars(select(UpgradeMaintenanceRow))).all():
                    fence.payload = active.model_dump(mode="json")
            claimed = datetime.now(UTC)
            owner = UpgradeExpiredRecoveryColdClaim(
                intent=intent,
                process=NodeAgentProcessIdentity(
                    pid=12346,
                    create_time_s=101.0,
                    environment_sha256=native.candidate.environment_sha256,
                ),
                runtime=native.candidate,
                kernel_boot_identity="synthetic-kernel",
                original_claim=proof.claim,
                original_native=native,
                previous_guard=NativeGuardStatus(
                    node=native.source.node,
                    bootstrap_runtime=native.source.runtime,
                    process=NodeAgentProcessIdentity(
                        pid=12345,
                        create_time_s=100.0,
                        environment_sha256=native.source.runtime.environment_sha256,
                    ),
                    selected=native.source,
                    ready=False,
                    observed_at=now,
                ),
                last_node=NativeNodeProcess(
                    selected=native.source, process=native.source_process, recorded_at=now
                ),
                supervision="fixture" if continued else "launchd",
                managed_definition_sha256=None if continued else "d" * 64,
                retired_supervisor=UpgradeRecoveryRetiredSupervisor(
                    pid=12348, create_time_s=100, owner_uid=501, script_sha256="d" * 64
                )
                if continued
                else None,
                claimed_at=claimed,
            )

            async def cold_accept():
                async with sessions.begin() as session:
                    return await cold.accept_cold_claim(session, owner, native.source.node)

            with monkeypatch.context() as patch:
                patch.setattr(cold, "write_principal_audit", unavailable_audit)
                with pytest.raises(RuntimeError):
                    await cold_accept()
            async with sessions.begin() as session:
                row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                assert row.phase == "accepted" and row.cold_claim_payload is None
                assert (
                    await cold.probe_cold_owner(
                        session,
                        intent.recovery_id,
                        owner.sha256(),
                        owner.process,
                        native.source.node,
                    )
                ).owner is None
            owners = await asyncio.gather(cold_accept(), cold_accept())
            assert sorted(created for _, created in owners) == [False, True]
            assert owners[0][0] == owners[1][0] == owner

            async def cold_gate(process=owner.process):
                async with sessions.begin() as session:
                    return await cold.verify_cold_owner(
                        session, intent.recovery_id, owner.sha256(), process, native.source.node
                    )

            assert await cold_gate() == owner
            from coire_core.models.upgrade_recovery import (
                UpgradeExpiredRecoveryColdSmokeCommand,
                UpgradeExpiredRecoveryColdSourceIntent,
                UpgradeExpiredRecoveryColdSourceLaunch,
                UpgradeExpiredRecoveryColdSourceProcess,
                UpgradeExpiredRecoveryColdSourceReceipt,
            )
            from coire_core.models.upgrades import NativeRuntimeSelection, NativeSmokeCommand

            selected_at = datetime.now(UTC)
            projection = UpgradeExpiredRecoveryColdSourceIntent(
                owner=owner,
                before_pointer=native.candidate,
                before_bootstrap_runtime=native.source.runtime,
                source_selection=NativeRuntimeSelection(
                    node=native.source.node,
                    runtime=native.source.runtime,
                    release_sha256=native.source.release_sha256,
                    job_id=native.job_id,
                    generation=native.generation,
                    selected_at=selected_at,
                ),
                projected_native=native.model_copy(
                    update={"phase": "source_selected", "updated_at": selected_at}
                ),
                prepared_at=selected_at,
            )
            source_receipt = UpgradeExpiredRecoveryColdSourceReceipt(
                projection=projection, observed_at=datetime.now(UTC)
            )
            launch = UpgradeExpiredRecoveryColdSourceLaunch(
                source=source_receipt, attempt=0, started_at=datetime.now(UTC)
            )
            source_process = UpgradeExpiredRecoveryColdSourceProcess(
                launch=launch,
                process=NodeAgentProcessIdentity(
                    pid=12347,
                    create_time_s=datetime.now(UTC).timestamp(),
                    environment_sha256=native.source.runtime.environment_sha256,
                ),
                observed_at=datetime.now(UTC),
            )
            if continued:
                from upgrade_cold_continuation_fixtures import exercise_continuation

                await exercise_continuation(
                    sessions,
                    actor,
                    owner,
                    source_process,
                    scope,
                    proof.native_data,
                    monkeypatch,
                    archived=archived_continued,
                    post_handback=post_handback_continued,
                    partial_handback=partial_handback_continued,
                    unstarted=unstarted_continued,
                    inherited=inherited_continued,
                )
                return
            smoke = UpgradeExpiredRecoveryColdSmokeCommand(
                source=source_process,
                command=NativeSmokeCommand(
                    plan=plan,
                    runtime_sha256=native.source.runtime.environment_sha256,
                    stage="source-serving",
                    deadline_at=intent.deadline_at,
                ),
                captured_at=datetime.now(UTC),
            )

            async def authorize_smoke():
                async with sessions.begin() as session:
                    return await cold.authorize_cold_smoke(session, smoke, native.source.node)

            with pytest.raises(UpgradeUnavailable, match="eligible local registry asset"):
                await authorize_smoke()
            node_id, copy_id = uuid4(), uuid4()
            async with sessions.begin() as session:
                session.add(
                    NodeRow(
                        id=node_id,
                        name=native.source.node,
                        role=NodeRole.STUDIO,
                        memory_total_bytes=8 * 1024**3,
                        disk_total_bytes=10 * 1024**3,
                        agent_version="fixture",
                        health_observed_at=datetime.now(UTC),
                        runtime_sha256=native.source.runtime.environment_sha256,
                    )
                )
                session.add(
                    ModelRow(
                        id=plan.model_id,
                        repo_id="fixture/tiny",
                        slug=plan.model_slug,
                        display_name="Tiny",
                        state=ModelState.READY,
                        precision="4bit",
                        weight_bytes=1024,
                        total_bytes=1024,
                        file_count=1,
                        memory_estimate_bytes=1024,
                    )
                )
                await session.flush()
                session.add(
                    ModelVariantRow(
                        id=plan.model_variant_id,
                        model_id=plan.model_id,
                        name="tiny",
                        slug=plan.model_slug,
                        source_revision="fixture",
                        precision="4bit",
                        state=VariantState.READY,
                        validated=True,
                        harness_verified=True,
                        is_default=True,
                        byte_size=1024,
                    )
                )
                await session.flush()
                session.add(
                    VariantCopyRow(
                        id=copy_id,
                        variant_id=plan.model_variant_id,
                        node_id=node_id,
                        path="/fixture/tiny",
                        role=CopyRole.REPLICA,
                        verified=True,
                        bytes=1024,
                        manifest_sha256=plan.model_manifest_sha256,
                    )
                )
            with monkeypatch.context() as patch:
                patch.setattr(cold, "write_principal_audit", unavailable_audit)
                with pytest.raises(RuntimeError, match="audit failure"):
                    await authorize_smoke()
            async with sessions.begin() as session:
                row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                assert row.cold_smoke_payload is None
            smoke_results = await asyncio.gather(authorize_smoke(), authorize_smoke())
            assert sorted(created for _, created in smoke_results) == [False, True]
            assert smoke_results[0][0] == smoke_results[1][0] == smoke
            async with sessions.begin() as session:
                copy = await session.get(VariantCopyRow, copy_id)
                copy.verified = False
            with pytest.raises(UpgradeUnavailable, match="matching verified manifests"):
                await authorize_smoke()
            async with sessions.begin() as session:
                copy = await session.get(VariantCopyRow, copy_id)
                copy.verified = True
            from coire_api.upgrades import expired_recovery_cold_handback as handback_service
            from coire_api.upgrades import expired_recovery_cold_verification as verifier
            from coire_core.models.upgrade_recovery import (
                UpgradeExpiredRecoveryColdCollectiveArchive,
                UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
                UpgradeExpiredRecoveryColdHandbackIntent,
                UpgradeExpiredRecoveryColdSmokeReceipt,
                UpgradeExpiredRecoveryColdVerificationRequest,
            )
            from coire_core.models.upgrades import (
                NODE_UPGRADE_OWNERS,
                NativeSmokeAttempt,
                NodeUpgradeInventory,
                UpgradeEvidence,
                UpgradeInventoryEntry,
                UpgradeOwnerInventory,
            )

            def inventory(peer=False, logical=False):
                entry = UpgradeInventoryEntry(
                    owner="probes",
                    kind="upgrade-collective",
                    work_id=str(scope.command_id),
                    state="cleaning",
                    holds=("unresolved-collective",),
                )
                return NodeUpgradeInventory(
                    job_id=native.job_id,
                    generation=native.generation,
                    node="coire-edge-b" if peer else native.source.node,
                    runtime_sha256=plan.peer_runtime_sha256
                    if peer
                    else native.source.runtime.environment_sha256,
                    observed_at=datetime.now(UTC),
                    owners=tuple(
                        UpgradeOwnerInventory(
                            owner=name,
                            status="complete",
                            entries=(entry,) if name == "probes" and logical else (),
                        )
                        for name in sorted(NODE_UPGRADE_OWNERS)
                    ),
                )

            archive = UpgradeExpiredRecoveryColdCollectiveArchive(
                source=source_process,
                scopes=(scope,),
                interrupted_scope_sha256s=(scope.sha256(),),
                local=inventory(logical=True),
                peer=inventory(True),
                history_sha256="e" * 64,
                prepared_at=datetime.now(UTC),
            )
            archived = UpgradeExpiredRecoveryColdCollectiveArchiveReceipt(
                initial_archive_sha256=archive.sha256(),
                archive=archive,
                archived_at=datetime.now(UTC),
            )
            serving = UpgradeExpiredRecoveryColdSmokeReceipt(
                smoke=smoke,
                attempt=NativeSmokeAttempt(
                    job_id=native.job_id,
                    generation=native.generation,
                    plan_sha256=plan.authority_sha256(),
                    stage="source-serving",
                    runtime_sha256=native.source.runtime.environment_sha256,
                    state="complete",
                    process=source_process.process,
                    cleanup_verified=True,
                    deadline_at=intent.deadline_at,
                    updated_at=datetime.now(UTC),
                    evidence=UpgradeEvidence(
                        check="node",
                        runtime_sha256=native.source.runtime.environment_sha256,
                        model_id=plan.model_id,
                        model_variant_id=plan.model_variant_id,
                        model_manifest_sha256=plan.model_manifest_sha256,
                        output_sha256="f" * 64,
                        generated_tokens=10,
                        cleanup_verified=True,
                    ),
                ),
                observed_at=datetime.now(UTC),
            )
            verification_request = UpgradeExpiredRecoveryColdVerificationRequest(
                smoke=serving,
                archive=archived,
                native_after=proof.native_data.model_copy(
                    update={"observed_at": datetime.now(UTC)}
                ),
            )

            async def observe_owners():
                # A separate transaction can immediately take the operation
                # mutex: the verifier closed its snapshot before transport I/O.
                from coire_api.upgrades.persistence import operation_lock

                async with asyncio.timeout(1), sessions.begin() as session:
                    await operation_lock(session)
                return inventory(), inventory(True)

            async def verify_source():
                return await verifier.prepare_verification(
                    sessions, native.source.node, verification_request, observe_owners
                )

            with monkeypatch.context() as patch:
                patch.setattr(verifier, "write_principal_audit", unavailable_audit)
                with pytest.raises(RuntimeError, match="audit failure"):
                    await verify_source()
            async with sessions.begin() as session:
                row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                assert row.cold_verification_payload is None and row.phase == "cold_recovering"
            verified_results = await asyncio.gather(verify_source(), verify_source())
            assert sorted(created for _, created in verified_results) == [False, True]
            verified = verified_results[0][0]
            assert verified_results[1][0] == verified
            assert await verify_source() == (verified, False)
            with pytest.raises(UpgradeUnavailable):
                await cold_gate()  # The new phase permits only the fixed handback.
            handback_started = datetime.now(UTC)
            handback = UpgradeExpiredRecoveryColdHandbackIntent(
                verification=verified,
                before_selection=native.source,
                before_runtime=owner.runtime,
                before_accepted_claim=owner.original_claim,
                released_claim=owner.original_claim.model_copy(
                    update={"state": "released", "updated_at": handback_started}
                ),
                started_at=handback_started,
            )

            async def accept_handback():
                async with sessions.begin() as session:
                    return await handback_service.accept_handback_intent(
                        session, handback, native.source.node
                    )

            with monkeypatch.context() as patch:
                patch.setattr(handback_service, "write_principal_audit", unavailable_audit)
                with pytest.raises(RuntimeError, match="audit failure"):
                    await accept_handback()
            handbacks = await asyncio.gather(accept_handback(), accept_handback())
            assert sorted(created for _, created in handbacks) == [False, True]
            assert handbacks[0][0] == handbacks[1][0] == handback
            from coire_core.models.upgrade_recovery import (
                UpgradeExpiredRecoveryColdHandbackHelper,
                UpgradeExpiredRecoveryColdHandbackReceipt,
            )

            helper_process = NodeAgentProcessIdentity(
                pid=12349,
                create_time_s=datetime.now(UTC).timestamp(),
                environment_sha256=native.source.runtime.environment_sha256,
            )
            handback_receipt = UpgradeExpiredRecoveryColdHandbackReceipt(
                helper=UpgradeExpiredRecoveryColdHandbackHelper(
                    handback=handback, process=helper_process, observed_at=datetime.now(UTC)
                ),
                status=NativeGuardStatus(
                    node=native.source.node,
                    bootstrap_runtime=native.source.runtime,
                    process=helper_process,
                    selected=verified.request.smoke.smoke.source.launch.source.projection.source_selection,
                    ready=True,
                    observed_at=datetime.now(UTC),
                ),
                node=NativeNodeProcess(
                    selected=verified.request.smoke.smoke.source.launch.source.projection.source_selection,
                    process=verified.request.smoke.smoke.source.process,
                    recorded_at=datetime.now(UTC),
                ),
                observed_at=datetime.now(UTC),
            )

            async def accept_handback_receipt():
                async with sessions.begin() as session:
                    return await handback_service.accept_handback_receipt(
                        session, handback_receipt, native.source.node
                    )

            with monkeypatch.context() as patch:
                patch.setattr(handback_service, "write_principal_audit", unavailable_audit)
                with pytest.raises(RuntimeError, match="audit failure"):
                    await accept_handback_receipt()
            async with sessions.begin() as session:
                row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                assert row.cold_handback_receipt_payload is None and row.phase == "handing_back"
            receipts = await asyncio.gather(accept_handback_receipt(), accept_handback_receipt())
            assert sorted(created for _, created in receipts) == [False, True]
            assert receipts[0][0] == receipts[1][0] == handback_receipt
            assert await accept_handback_receipt() == (handback_receipt, False)
            async with sessions.begin() as session:
                row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                assert row.phase == "resolving" and row.active_slot == 1
            if resolve:
                from coire_api.upgrades import expired_recovery_cold_resolution as resolver
                from coire_core.models.upgrade_recovery import (
                    UpgradeExpiredRecoveryColdClosure,
                    UpgradeExpiredRecoveryColdResolutionObservation,
                    UpgradeExpiredRecoveryColdResolutionRead,
                    UpgradeExpiredRecoveryColdResolutionRequest,
                    UpgradeExpiredRecoveryColdSupervisionIntent,
                    UpgradeExpiredRecoveryColdSupervisionReceipt,
                )
                from coire_core.models.upgrades import NodeMaintenanceAck

                closed_at = datetime.now(UTC)
                closure = UpgradeExpiredRecoveryColdClosure(
                    handback=handback_receipt,
                    supervision=UpgradeExpiredRecoveryColdSupervisionReceipt(
                        supervision=UpgradeExpiredRecoveryColdSupervisionIntent(
                            handback=handback_receipt, started_at=closed_at
                        ),
                        process=handback_receipt.helper.process,
                        kind="launchd",
                        definition_sha256=owner.managed_definition_sha256,
                        observed_at=closed_at,
                    ),
                    local_release=NodeMaintenanceAck(
                        job_id=operation.job_id,
                        generation=operation.generation,
                        node=native.source.node,
                        applied_at=closed_at,
                        released=True,
                    ),
                    status=handback_receipt.status.model_copy(update={"observed_at": closed_at}),
                    node=handback_receipt.node,
                    observed_at=closed_at,
                )
                peer_release = NodeMaintenanceAck(
                    job_id=operation.job_id,
                    generation=operation.generation,
                    node="coire-edge-b",
                    applied_at=closed_at,
                    released=True,
                )
                resolution_request = UpgradeExpiredRecoveryColdResolutionRequest(
                    closure=closure,
                    observation=UpgradeExpiredRecoveryColdResolutionObservation(
                        closure_sha256=closure.sha256(),
                        status=closure.status,
                        node=closure.node,
                        maintenance=plan.maintenance.model_copy(
                            update={"state": "released", "updated_at": closed_at}
                        ),
                        observed_at=closed_at,
                    ),
                )
                state_read = UpgradeExpiredRecoveryColdResolutionRead(
                    intent_sha256=intent.sha256(), closure_sha256=closure.sha256()
                )
                async with sessions.begin() as session:
                    assert (
                        await resolver.read_resolution_state(
                            session, intent.recovery_id, state_read, native.source.node
                        )
                    ).resolution is None

                async def finish():
                    async with sessions.begin() as session:
                        return await resolver.resolve_cold_recovery(
                            session, resolution_request, native.source.node, peer_release
                        )

                from coire_api.nodes_client import NodeError, NodeErrorKind
                from coire_api.upgrades.expired_recovery_cold_resolution_orchestration import (
                    resolve_with_observation,
                )
                from coire_api.upgrades.persistence import operation_lock
                from coire_core.models.upgrades import NodeUpgradeStatus

                observed_nodes = []
                releases = []
                transport_fault = None

                async def physical_status(selected_node):
                    # An independent transaction must obtain the operation
                    # mutex during every transport call, including peer I/O.
                    async with asyncio.timeout(1), sessions.begin() as session:
                        await operation_lock(session)
                    observed_nodes.append(selected_node)
                    if transport_fault == "unreachable":
                        raise NodeError(NodeErrorKind.UNREACHABLE, selected_node)
                    return NodeUpgradeStatus(
                        node=selected_node,
                        runtime_sha256=(
                            "f" * 64
                            if transport_fault == "foreign"
                            else native.source.runtime.environment_sha256
                            if selected_node == native.source.node
                            else plan.peer_runtime_sha256
                        ),
                        release_sha256=intent.request.expected_source_sha256,
                        bootstrap_ready=True,
                        observed_at=datetime.now(UTC)
                        - timedelta(seconds=10 if transport_fault == "stale" else 0),
                    )

                async def release_peer(selected_node, command):
                    async with asyncio.timeout(1), sessions.begin() as session:
                        await operation_lock(session)
                    releases.append(command)
                    assert selected_node == "coire-edge-b"
                    assert command.maintenance == plan.maintenance
                    assert command.action == "release" and command.terminal_verified
                    if transport_fault == "lost_release":
                        raise NodeError(NodeErrorKind.UNREACHABLE, selected_node)
                    return peer_release

                nodes = SimpleNamespace(
                    upgrade_status=physical_status, apply_upgrade_maintenance=release_peer
                )

                async def orchestrated_finish():
                    return await resolve_with_observation(
                        sessions, resolution_request, native.source.node, nodes
                    )

                from coire_core.errors import UpgradeConflict

                for transport_fault in ("unreachable", "foreign", "stale", "lost_release"):
                    error = (
                        UpgradeUnavailable
                        if transport_fault in {"unreachable", "lost_release"}
                        else UpgradeConflict
                    )
                    with pytest.raises(error):
                        await orchestrated_finish()
                    async with sessions.begin() as session:
                        row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                        original = await session.get(UpgradeOperationRow, operation.job_id)
                        assert row.resolution_payload is None and row.active_slot == 1
                        assert original.active_slot == 1
                        assert all(
                            not fence.released
                            for fence in (
                                await session.scalars(select(UpgradeMaintenanceRow))
                            ).all()
                        )
                transport_fault = None

                with monkeypatch.context() as patch:
                    patch.setattr(resolver, "write_principal_audit", unavailable_audit)
                    with pytest.raises(RuntimeError, match="audit failure"):
                        await finish()
                async with sessions.begin() as session:
                    row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                    assert row.resolution_payload is None and row.phase == "resolving"
                    original = await session.get(UpgradeOperationRow, operation.job_id)
                    assert original.payload == operation.model_dump(mode="json")
                    assert original.active_slot == 1
                    assert all(
                        not fence.released
                        for fence in (await session.scalars(select(UpgradeMaintenanceRow))).all()
                    )
                results = await asyncio.gather(orchestrated_finish(), orchestrated_finish())
                assert sorted(created for _, created in results) == [False, True]
                resolution = results[0][0]
                assert results[1][0] == resolution
                assert resolution.operation.phase is UpgradePhase.ROLLED_BACK
                assert resolution.outcome == "recovered_after_deadline"
                assert resolution.original_upgrade_succeeded is False
                assert resolution.before_operation == operation
                import json

                from anyio import Path

                packet = Path(tmp_path / "retained-reader-packet.json")
                await packet.write_text(
                    json.dumps(
                        {
                            "operation": resolution.operation.model_dump(mode="json"),
                            "event": resolution.event.model_dump(mode="json"),
                        }
                    )
                )
                await packet.chmod(0o600)
                assert resolution.operation.authority_sha256() == operation.authority_sha256()
                assert resolution.event.effect_id == f"expired-recovery-{intent.recovery_id}"
                io_count = len(observed_nodes), len(releases)
                assert await orchestrated_finish() == (resolution, False)
                assert (len(observed_nodes), len(releases)) == io_count
                assert await finish() == (resolution, False)
                from coire_api.upgrades import expired_recovery_host_hold as host_recovery
                from coire_api.upgrades.host_hold import operation_hold
                from coire_core.deploy import host_boot_identity
                from coire_core.models.upgrade_recovery import UpgradeRecoveryHostHoldRequest
                from coire_core.models.upgrades import DeploymentOperationHold, DeploymentOwnership
                from coire_core.settings import Settings
                from coire_core.upgrade_journal import PrivateUpgradeStore

                coordination = PrivateUpgradeStore(tmp_path / "terminal-coordination")
                coordination.write(
                    "ownership.json",
                    DeploymentOwnership(
                        project="coire",
                        owner_kind="host",
                        owner_identity="qualified-host",
                        boot_identity=host_boot_identity(),
                        generation=1,
                        state="committed",
                        stage="selected",
                    ),
                )
                host_settings = Settings(upgrade_coordination_dir=str(coordination.root))
                operation_hold(host_settings, plan)

                async def reconcile_hold(body):
                    async with sessions.begin() as session:
                        return await host_recovery.reconcile_host_hold(
                            session, actor, host_settings, intent.recovery_id, body
                        )

                hold_request = UpgradeRecoveryHostHoldRequest(resolution_sha256=resolution.sha256())
                with pytest.raises(UpgradeConflict):
                    await reconcile_hold(
                        hold_request.model_copy(update={"resolution_sha256": "a" * 64})
                    )
                assert not coordination.read(
                    "operation-hold.json", DeploymentOperationHold
                ).released
                with monkeypatch.context() as patch:
                    patch.setattr(host_recovery, "write_principal_audit", unavailable_audit)
                    with pytest.raises(RuntimeError, match="audit failure"):
                        await reconcile_hold(hold_request)
                assert coordination.read("operation-hold.json", DeploymentOperationHold).released
                held_results = await asyncio.gather(
                    reconcile_hold(hold_request), reconcile_hold(hold_request)
                )
                assert sorted(created for _, created in held_results) == [False, True]
                assert held_results[0][0] == held_results[1][0]
                assert await reconcile_hold(hold_request) == (held_results[0][0], False)
                from coire_api.upgrades import native_terminal as native_ack
                from coire_core.models.upgrade_native_terminal import UpgradeNativeTerminalRequest

                async def acknowledge_native(body):
                    async with sessions.begin() as session:
                        return await native_ack.prepare_native_terminal(
                            session, actor, intent.recovery_id, body
                        )

                # A later source-preserving cancellation has replaced the per-target
                # ledger rows. Its real terminal/released epoch must also be accepted.
                later_job = "01ARZ3NDEKTSV4RRFFQ69G5FAZ"
                later = plan.operation.model_copy(
                    update={
                        "job_id": later_job,
                        "generation": operation.generation + 1,
                        "phase": UpgradePhase.CANCELLED_UNCHANGED,
                        "finished_at": datetime.now(UTC),
                        "updated_at": datetime.now(UTC),
                        "idempotency_key": uuid4(),
                    }
                )
                async with sessions.begin() as session:
                    session.add(
                        UpgradeOperationRow(
                            id=later_job,
                            user_id=operation.user_id,
                            source_release_id=plan.source.release_id,
                            target_release_id=plan.target.release_id,
                            idempotency_key=later.idempotency_key,
                            request_sha256=later.request_sha256,
                            generation=later.generation,
                            active_slot=None,
                            phase=later.phase.value,
                            payload=later.model_dump(mode="json"),
                        )
                    )
                    await session.flush()
                    for fence in (await session.scalars(select(UpgradeMaintenanceRow))).all():
                        fence.job_id = later_job
                        fence.generation = later.generation
                        fence.released = True
                        fence.payload = plan.maintenance.model_copy(
                            update={
                                "job_id": later_job,
                                "generation": later.generation,
                                "state": "released",
                            }
                        ).model_dump(mode="json")
                native_body = UpgradeNativeTerminalRequest(resolution_sha256=resolution.sha256())
                with pytest.raises(UpgradeConflict):
                    await acknowledge_native(
                        native_body.model_copy(update={"resolution_sha256": "a" * 64})
                    )
                with monkeypatch.context() as patch:
                    patch.setattr(native_ack, "write_principal_audit", unavailable_audit)
                    with pytest.raises(RuntimeError, match="audit failure"):
                        await acknowledge_native(native_body)
                first_native, replay_native = await asyncio.gather(
                    acknowledge_native(native_body), acknowledge_native(native_body)
                )
                assert first_native == replay_native and first_native.resolution == resolution
                async with sessions.begin() as session:
                    assert (
                        await native_ack.native_terminal_state(
                            session, intent.recovery_id, native.source.node
                        )
                    ).grant == first_native
                    with pytest.raises(UpgradeForbidden):
                        await native_ack.native_terminal_state(
                            session, intent.recovery_id, "coire-edge-b"
                        )
                async with sessions.begin() as session:
                    assert (
                        await resolver.read_resolution_state(
                            session, intent.recovery_id, state_read, native.source.node
                        )
                    ).resolution == resolution
                    user = await session.get(UserRow, operation.user_id)
                    user.role = UserRole.USER
                async with sessions.begin() as session:
                    # Exact terminal metadata grants no effects and remains
                    # readable by the fixed node after initiator revocation.
                    assert (
                        await resolver.read_resolution_state(
                            session, intent.recovery_id, state_read, native.source.node
                        )
                    ).resolution == resolution
                with pytest.raises(UpgradeForbidden):
                    await finish()
                async with sessions.begin() as session:
                    user = await session.get(UserRow, operation.user_id)
                    user.role = UserRole.ADMIN
                from coire_core.errors import UpgradeConflict

                with pytest.raises(
                    UpgradeConflict, match="Original recovery owner binding changed"
                ):
                    await cold_gate()
                async with sessions.begin() as session:
                    row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                    assert row.active_slot is None and row.phase == "recovered"
                    assert row.cold_handback_receipt_payload == handback_receipt.model_dump(
                        mode="json"
                    )
                    assert (
                        await read_recovery_status(session, actor, intent.recovery_id)
                    ).active is False
                return
            with pytest.raises(UpgradeForbidden):
                await cold_gate(owner.process.model_copy(update={"pid": 99999}))
            with monkeypatch.context() as patch:
                patch.setattr(
                    cold, "datetime", SimpleNamespace(now=lambda zone: intent.deadline_at)
                )
                assert await cold_accept() == (owner, False)
                with pytest.raises(UpgradeUnavailable):
                    await cold_gate()
            async with sessions.begin() as session:
                await cancel_recovery(
                    session,
                    actor,
                    intent.recovery_id,
                    UpgradeExpiredRecoveryCancelRequest(intent_sha256=intent.sha256()),
                )
            with pytest.raises(UpgradeUnavailable):
                await cold_gate()
            async with sessions.begin() as session:
                row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
                assert (
                    row.cold_claim_payload == owner.model_dump(mode="json") and row.active_slot == 1
                )
                assert (
                    row.transfer_payload is None
                    and row.consumption_payload is None
                    and row.owner_claim_payload is None
                )
                assert (
                    len(
                        (
                            await session.scalars(
                                select(AuditRow).where(
                                    AuditRow.action == "upgrades.expired_recovery.cold_owner.accept"
                                )
                            )
                        ).all()
                    )
                    == 1
                )
        async with sessions.begin() as session:
            assert (
                len(
                    (
                        await session.scalars(
                            select(AuditRow).where(
                                AuditRow.action == "upgrades.expired_recovery.accept"
                            )
                        )
                    ).all()
                )
                == 1
            )
            original = await session.get(UpgradeOperationRow, operation.job_id)
            assert (
                original.payload == operation.model_dump(mode="json") and original.active_slot == 1
            )
            assert all(
                not fence.released
                for fence in (await session.scalars(select(UpgradeMaintenanceRow))).all()
            )
            user = await session.get(UserRow, operation.user_id)
            user.role = UserRole.USER
        with pytest.raises(UpgradeForbidden):
            await accept()
    finally:
        await engine.dispose()
        async with root.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await root.dispose()


async def exercise_unstarted_closure(
    sessions,
    actor,
    intent,
    original_proof,
    native,
    operation,
    monkeypatch,
    *,
    cancelled=False,
    pre_projection=False,
):
    """Actual audit/slot transactions; native observations remain fixture facts."""
    from types import SimpleNamespace

    from coire_api.upgrades import expired_recovery_unstarted as worker
    from coire_core.errors import UpgradeConflict, UpgradeUnavailable
    from coire_core.models.upgrade_recovery import (
        UpgradeExpiredRecoverySupportProcess,
        UpgradeExpiredRecoveryUnstartedCloseRequest,
        UpgradeExpiredRecoveryUnstartedObservation,
    )
    from coire_core.models.upgrades import (
        NativeGuardStatus,
        NativeNodeProcess,
        NodeAgentProcessIdentity,
    )

    expired_at = intent.deadline_at + timedelta(seconds=1)
    if cancelled:
        from coire_api.upgrades.expired_recovery_cancel import cancel_recovery
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryCancelRequest

        async with sessions.begin() as session:
            cancellation, created = await cancel_recovery(
                session,
                actor,
                intent.recovery_id,
                UpgradeExpiredRecoveryCancelRequest(intent_sha256=intent.sha256()),
            )
            assert created
            assert cancellation.intent_sha256 == intent.sha256()
    async with sessions.begin() as session:
        active = intent.original_plan.maintenance.model_copy(
            update={
                "state": "active",
                "acknowledged_generations": dict.fromkeys(
                    intent.original_plan.maintenance.targets, operation.generation
                ),
            }
        )
        for fence in (await session.scalars(select(UpgradeMaintenanceRow))).all():
            fence.payload = active.model_dump(mode="json")
    runtime = original_proof.observer_runtime
    startup = UpgradeExpiredRecoverySupportProcess(
        intent=intent,
        runtime=runtime,
        process=NodeAgentProcessIdentity(
            pid=123455,
            create_time_s=intent.accepted_at.timestamp(),
            environment_sha256=runtime.environment_sha256,
        ),
        observed_at=intent.accepted_at,
    )
    proof = UpgradeExpiredRecoveryUnstartedObservation(
        intent=intent,
        startup=startup,
        observer_release=intent.support_release,
        observer_runtime=runtime,
        observer=NodeAgentProcessIdentity(
            pid=123456,
            create_time_s=expired_at.timestamp(),
            environment_sha256=runtime.environment_sha256,
        ),
        native=native,
        claim=original_proof.claim,
        guard=NativeGuardStatus(
            node=native.source.node,
            bootstrap_runtime=native.source.runtime,
            process=native.source_process,
            selected=native.source,
            ready=False,
            observed_at=expired_at,
        ),
        node=NativeNodeProcess(
            selected=native.source, process=native.source_process, recorded_at=expired_at
        ),
        pointer=native.source.runtime,
        bootstrap_runtime=runtime.model_copy(
            update={"environment_id": "independent-helper", "environment_sha256": "f" * 64}
        ),
        native_data=original_proof.native_data.model_copy(update={"observed_at": expired_at}),
        observed_at=expired_at,
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            worker,
            "datetime",
            SimpleNamespace(now=lambda _: intent.accepted_at + timedelta(seconds=1)),
        )
        async with sessions.begin() as session:
            with pytest.raises(UpgradeConflict, match="expired attempt"):
                await worker.unstarted_context(
                    session, intent.recovery_id, intent.sha256(), native.source.node
                )
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: expired_at))
    monkeypatch.setattr(
        "coire_api.upgrades.expired_recovery_data.datetime",
        SimpleNamespace(now=lambda _: expired_at),
    )
    relay = worker.UnstartedObservationRelay()
    async with sessions.begin() as session:
        row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
        row.cold_claim_payload = {"fixture": "issued authority"}
        await session.flush()
        with pytest.raises(UpgradeConflict, match="without issued effect authority"):
            await worker.unstarted_context(
                session, intent.recovery_id, intent.sha256(), native.source.node
            )
        # Restore SQL NULL, not JSON's null literal, after this injected
        # authority fault. The database absence constraint is intentionally
        # stricter than Python's decoded JSON None.
        from sqlalchemy import null

        row.cold_claim_payload = null()
    if pre_projection:
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdClaim

        cold = UpgradeExpiredRecoveryColdClaim(
            intent=intent,
            runtime=runtime,
            process=startup.process,
            kernel_boot_identity="synthetic-kernel",
            original_claim=proof.claim,
            original_native=proof.native,
            previous_guard=proof.guard,
            last_node=proof.node,
            supervision="launchd",
            managed_definition_sha256="d" * 64,
            claimed_at=intent.accepted_at,
        )
        proof = proof.model_copy(update={"pre_projection_owner": cold})
        async with sessions.begin() as session:
            row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            row.cold_claim_payload = cold.model_dump(mode="json")
    async with sessions.begin() as session:
        await worker.verify_unstarted_observation(session, proof, native.source.node)
        with pytest.raises(UpgradeForbidden):
            await worker.verify_unstarted_observation(session, proof, "coire-edge-b")
        changed = proof.model_copy(
            update={"native_data": proof.native_data.model_copy(update={"total_bytes": 1})}
        )
        with pytest.raises(UpgradeConflict, match="native data changed"):
            await worker.verify_unstarted_observation(session, changed, native.source.node)
    relay.publish(proof)
    request = UpgradeExpiredRecoveryUnstartedCloseRequest(
        intent_sha256=intent.sha256(),
        observation_sha256=proof.sha256(),
        idempotency_key="coire_123456789012_abcdefghijklmnopqrstuvwx",
    )

    async def close():
        async with sessions.begin() as session:
            return await worker.close_unstarted_recovery(
                session, actor, intent.recovery_id, request, relay
            )

    async with sessions.begin() as session:
        user = await session.get(UserRow, operation.user_id)
        user.role = UserRole.USER
    with pytest.raises(UpgradeForbidden):
        await close()
    async with sessions.begin() as session:
        user = await session.get(UserRow, operation.user_id)
        user.role = UserRole.ADMIN

    async def unavailable(*args, **kwargs):
        raise RuntimeError("fixture closure audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(worker, "write_principal_audit", unavailable)
        with pytest.raises(RuntimeError, match="audit failure"):
            await close()
    async with sessions.begin() as session:
        row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
        assert row.active_slot == 1 and row.phase == (
            "recovery_required" if cancelled else "accepted"
        )
    results = await asyncio.gather(close(), close())
    assert sorted(created for _, created in results) == [False, True]
    assert results[0][0] == results[1][0]
    assert results[0][0].original_maintenance_retained
    assert not results[0][0].original_upgrade_succeeded
    # Lost response replay reads the audited receipt without needing the expired
    # process-local sample, recapturing a deadline or touching native state.
    relay._entries.clear()
    assert await close() == (results[0][0], False)
    async with sessions.begin() as session:
        changed_request = request.model_copy(update={"observation_sha256": "f" * 64})
        with pytest.raises(UpgradeConflict, match="replay differs"):
            await worker.close_unstarted_recovery(
                session, actor, intent.recovery_id, changed_request, relay
            )
    async with sessions.begin() as session:
        row = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
        original = await session.get(UpgradeOperationRow, operation.job_id)
        assert row.active_slot is None and row.phase == (
            "superseded" if pre_projection else "cancelled_unchanged"
        )
        if pre_projection:
            assert row.cold_claim_payload == cold.model_dump(mode="json")
            assert row.resolution_payload["owner_sha256"] == cold.sha256()
        if cancelled:
            assert row.cancellation_payload == cancellation.model_dump(mode="json")
        assert row.intent_payload == intent.model_dump(mode="json")
        assert original.active_slot == 1 and original.payload == operation.model_dump(mode="json")
        assert all(
            not fence.released
            for fence in (await session.scalars(select(UpgradeMaintenanceRow))).all()
        )
        assert (
            len(
                (
                    await session.scalars(select(AuditRow).where(AuditRow.action == worker.ACTION))
                ).all()
            )
            == 1
        )
    with pytest.raises(UpgradeUnavailable, match="absent or stale"):
        relay.read(proof.sha256())
    # Actual issued API key, ordinary middleware/role scope and route replay.
    import httpx
    from pydantic import SecretStr

    from coire_api import db
    from coire_api.app import create_app
    from coire_api.identity.keys import issue_key
    from coire_core.models.auth import ApiKeyCreate, AuthScope
    from coire_core.settings import Settings

    async with sessions.begin() as session:
        key = await issue_key(
            session,
            operation.user_id,
            ApiKeyCreate(
                name="Unstarted closure fixture",
                scopes=frozenset({AuthScope.ADMIN}),
                requests_per_minute=100,
                monthly_budget_tokens=1000,
            ),
            actor="fixture",
        )
    monkeypatch.setattr(db, "_sessionmaker", sessions)
    monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
    app = create_app(
        Settings(upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}'))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://unstarted.internal"
    ) as client:
        route = f"/api/v1/admin/upgrades/recoveries/{intent.recovery_id}/close-unstarted"
        response = await client.post(
            route,
            json=request.model_dump(mode="json"),
            headers={"Authorization": "Bearer " + key.secret},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == results[0][0].model_dump(mode="json")
        assert (await client.post(route, json=request.model_dump(mode="json"))).status_code == 401
        async with sessions.begin() as session:
            (await session.get(UserRow, operation.user_id)).role = UserRole.USER
        assert (
            await client.post(
                route,
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + key.secret},
            )
        ).status_code == 403
