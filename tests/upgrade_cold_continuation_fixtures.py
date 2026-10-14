"""Controlled native metadata for actual PostgreSQL continuation transactions."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from coire_api.db import (
    UpgradeExpiredRecoveryRow,
    UpgradeMaintenanceRow,
    UpgradeOperationRow,
    UpgradeRecoverySuccessionRow,
)
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryCancelRequest
from coire_core.models.upgrade_recovery_cold_binding import UpgradeColdContinuationBinding
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationObservation,
    UpgradeColdContinuationRead,
    UpgradeColdContinuationRequest,
)
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    NativeGuardStatus,
    NativeNodeProcess,
    NodeAgentProcessIdentity,
    NodeUpgradeInventory,
    UpgradeInventoryEntry,
    UpgradeOwnerInventory,
)


def failed_pair_after_archive(source, archive, at):
    """Synthetic prior serving/failure records for metadata and transaction tests."""
    from uuid import NAMESPACE_URL, uuid4, uuid5

    from coire_core.models.sharding import (
        LinkObservation,
        LinkProbeCommand,
        ProbeOutcome,
        ProbeTransport,
    )
    from coire_core.models.upgrade_recovery import (
        UpgradeExpiredRecoveryColdPairCommand,
        UpgradeExpiredRecoveryColdPairResult,
        UpgradeExpiredRecoveryColdSmokeCommand,
        UpgradeExpiredRecoveryColdSmokeReceipt,
    )
    from coire_core.models.upgrades import NativeSmokeAttempt, NativeSmokeCommand, UpgradeEvidence

    intent = source.launch.source.projection.owner.intent
    plan = intent.original_plan
    command = UpgradeExpiredRecoveryColdSmokeCommand(
        source=source,
        command=NativeSmokeCommand(
            plan=plan,
            stage="source-serving",
            runtime_sha256=plan.source.manifest.environment_sha256,
            deadline_at=intent.deadline_at,
        ),
        captured_at=at,
    )
    smoke = UpgradeExpiredRecoveryColdSmokeReceipt(
        smoke=command,
        attempt=NativeSmokeAttempt(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            plan_sha256=plan.authority_sha256(),
            stage="source-serving",
            runtime_sha256=command.command.runtime_sha256,
            deadline_at=intent.deadline_at,
            state="complete",
            process=source.process,
            cleanup_verified=True,
            evidence=UpgradeEvidence(
                check="node",
                runtime_sha256=command.command.runtime_sha256,
                model_id=plan.model_id,
                model_variant_id=plan.model_variant_id,
                model_manifest_sha256=plan.model_manifest_sha256,
                generated_tokens=10,
                output_sha256="c" * 64,
                cleanup_verified=True,
            ),
            updated_at=at,
        ),
        observed_at=at,
    )
    dispatch = UpgradeExpiredRecoveryColdPairCommand(
        smoke=smoke,
        archive=archive,
        index=0,
        command=LinkProbeCommand(
            command_id=uuid5(
                NAMESPACE_URL,
                f"coire-expired-recovery-pair/{intent.recovery_id}/{intent.sha256()}/0",
            ),
            transport=ProbeTransport.JACCL,
            hostfile_sha256="d" * 64,
        ),
        captured_at=at,
    )
    result = UpgradeExpiredRecoveryColdPairResult(
        dispatch=dispatch,
        observation=LinkObservation(
            id=uuid4(),
            node_a="coire-edge-a",
            node_b="coire-edge-b",
            transport=ProbeTransport.JACCL,
            outcome=ProbeOutcome.FAILED,
            os_version_a="synthetic",
            os_version_b="synthetic",
            engine_version="synthetic",
            reason="synthetic incomplete ranks",
            observed_at=at,
        ),
    )
    return smoke, result


async def exercise_continuation(
    sessions,
    actor,
    owner,
    source,
    scope,
    native_baseline,
    monkeypatch,
    *,
    archived=False,
    post_handback=False,
    unstarted=False,
    inherited=False,
):
    from coire_api.upgrades import cold_continuation_accept as service
    from coire_api.upgrades import cold_continuation_context as context
    from coire_api.upgrades import cold_continuation_relay as relay_module
    from coire_api.upgrades import cold_unstarted_lineage as lineage
    from coire_api.upgrades import expired_recovery as accept_service
    from coire_api.upgrades import expired_recovery_cold as cold
    from coire_api.upgrades import expired_recovery_data as data
    from coire_api.upgrades.cold_continuation_binding import retained_binding
    from coire_api.upgrades.expired_recovery_cancel import cancel_recovery
    from coire_api.upgrades.expired_recovery_status import read_recovery_status
    from coire_core.errors import UpgradeConflict
    from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdClaim

    intent = owner.intent
    plan = intent.original_plan
    now = intent.deadline_at + timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=UTC):
            return now

    for module in (service, context, relay_module, accept_service, cold, data, lineage):
        monkeypatch.setattr(module, "datetime", Clock)
    async with sessions.begin() as session:
        await cancel_recovery(
            session,
            actor,
            intent.recovery_id,
            UpgradeExpiredRecoveryCancelRequest(intent_sha256=intent.sha256()),
        )
    read = UpgradeColdContinuationRead(
        previous_intent_sha256=intent.sha256(), previous_owner_sha256=owner.sha256()
    )
    async with sessions.begin() as session:
        metadata = await context.read_cold_predecessor_context(
            session, intent.recovery_id, read, plan.operation.node
        )
        original = await session.get(UpgradeOperationRow, plan.operation.job_id)
        original_bytes = (original.payload, original.execution_plan)
    guard = owner.previous_guard.model_copy(
        update={
            "process": owner.process,
            "selected": source.launch.source.projection.source_selection,
            "bootstrap_runtime": owner.runtime,
        }
    )
    node = NativeNodeProcess(
        selected=source.launch.source.projection.source_selection,
        process=source.process,
        recorded_at=source.observed_at,
    )
    binding = UpgradeColdContinuationBinding(
        previous_recovery_id=intent.recovery_id,
        previous_intent_sha256=intent.sha256(),
        previous_owner_sha256=owner.sha256(),
        previous_source_sha256=source.sha256(),
        previous_deadline_at=intent.deadline_at,
        previous_kernel_boot_identity=owner.kernel_boot_identity,
        retired_supervisor_sha256=owner.retired_supervisor.sha256(),
        original_claim=owner.original_claim,
        original_native=owner.original_native,
        projected_native=source.launch.source.projection.projected_native,
        previous_guard=guard,
        retained_node=node,
        core_data_baseline_sha256=metadata.data_baseline.sha256(),
        native_data_baseline_sha256=native_baseline.sha256(),
        observed_at=now,
    )
    logical = UpgradeInventoryEntry(
        owner="probes",
        kind="upgrade-collective",
        work_id=str(scope.command_id),
        state="cleaning",
        holds=("unresolved-collective",),
    )

    def inventory(peer=False):
        return NodeUpgradeInventory(
            job_id=plan.operation.job_id,
            generation=plan.operation.generation,
            node="coire-edge-b" if peer else plan.operation.node,
            runtime_sha256=plan.peer_runtime_sha256
            if peer
            else owner.original_native.source.runtime.environment_sha256,
            owners=tuple(
                UpgradeOwnerInventory(
                    owner=name,
                    status="complete",
                    entries=(logical,) if name == "probes" and not peer else (),
                )
                for name in sorted(NODE_UPGRADE_OWNERS)
            ),
            observed_at=now,
        )

    previous_archive = previous_smoke = None
    previous_pair_results = None
    if archived:
        from coire_core.models.upgrade_recovery import (
            UpgradeExpiredRecoveryColdCollectiveArchive,
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )

        at = source.observed_at + timedelta(seconds=1)
        archive_body = UpgradeExpiredRecoveryColdCollectiveArchive(
            source=source,
            scopes=(scope,),
            interrupted_scope_sha256s=(scope.sha256(),),
            local=inventory().model_copy(update={"observed_at": at}),
            peer=inventory(True).model_copy(update={"observed_at": at}),
            history_sha256="d" * 64,
            prepared_at=at,
        )
        previous_archive = UpgradeExpiredRecoveryColdCollectiveArchiveReceipt(
            archive=archive_body, initial_archive_sha256=archive_body.sha256(), archived_at=at
        )
        previous_smoke, failed = failed_pair_after_archive(source, previous_archive, at)
        previous_pair_results = (failed,)
        binding = UpgradeColdContinuationBinding.model_validate(
            binding.model_dump(mode="json")
            | {
                "previous_archive_sha256": previous_archive.sha256(),
                "previous_history_sha256": archive_body.history_sha256,
                "previous_smoke_sha256": previous_smoke.sha256(),
                "previous_pair_result_sha256s": [failed.sha256()],
            }
        )
        async with sessions.begin() as session:
            predecessor = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            predecessor.cold_smoke_payload = previous_smoke.smoke.model_dump(mode="json")

    local = inventory()
    if archived:
        local = local.model_copy(
            update={"owners": tuple(o.model_copy(update={"entries": ()}) for o in local.owners)}
        )
    if post_handback:
        from coire_core.models.upgrade_recovery_cold_binding import UpgradeColdPostHandbackBinding

        at = source.observed_at + timedelta(seconds=1)
        old_local = local.model_copy(update={"observed_at": at})
        old_peer = inventory(True).model_copy(update={"observed_at": at})
        previous_smoke, qualification, handback = completed_handback_after_archive(
            source,
            previous_archive,
            at,
            metadata.data_baseline,
            native_baseline,
            old_local,
            old_peer,
        )
        previous_pair_results = qualification.results
        guard = handback.status
        binding = UpgradeColdContinuationBinding.model_validate(
            binding.model_dump(mode="json")
            | {
                "previous_guard": guard.model_dump(mode="json"),
                "previous_smoke_sha256": previous_smoke.sha256(),
                "previous_pair_result_sha256s": [r.sha256() for r in qualification.results],
                "previous_pair_qualification_sha256": qualification.sha256(),
                "previous_verification_request_sha256": handback.helper.handback.verification.request.sha256(),
                "post_handback": UpgradeColdPostHandbackBinding(
                    handback_sha256=handback.sha256(),
                    supervision_sha256="e" * 64,
                    closure_intent_sha256="f" * 64,
                    released_claim=handback.helper.handback.released_claim,
                    released_maintenance=plan.maintenance.model_copy(
                        update={"state": "released", "updated_at": at}
                    ),
                    source_helper=guard,
                    retired_supervisor=NodeAgentProcessIdentity(
                        pid=12352,
                        create_time_s=at.timestamp(),
                        environment_sha256=owner.runtime.environment_sha256,
                    ),
                ).model_dump(mode="json"),
            }
        )
        async with sessions.begin() as session:
            predecessor = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            predecessor.cold_smoke_payload = previous_smoke.smoke.model_dump(mode="json")
            predecessor.cold_verification_payload = (
                handback.helper.handback.verification.model_dump(mode="json")
            )
            predecessor.cold_handback_intent_payload = handback.helper.handback.model_dump(
                mode="json"
            )
            predecessor.cold_handback_receipt_payload = handback.model_dump(mode="json")
    proof = UpgradeColdContinuationObservation(
        binding=binding,
        previous_owner=owner,
        previous_source=source,
        previous_archive=previous_archive,
        previous_smoke=previous_smoke,
        previous_pair_results=previous_pair_results,
        observer_release=intent.support_release,
        observer_runtime=owner.runtime,
        observer=NodeAgentProcessIdentity(
            pid=12349,
            create_time_s=now.timestamp(),
            environment_sha256=owner.runtime.environment_sha256,
        ),
        native_baseline=native_baseline,
        native_data=native_baseline.model_copy(update={"observed_at": now}),
        scopes=(scope,),
        interrupted_scope_sha256s=(scope.sha256(),),
        local=local,
        peer=inventory(True),
        observed_at=now,
    )
    async with sessions.begin() as session:
        await relay_module.verify_observation_context(session, proof, plan.operation.node)
    relay = relay_module.ColdContinuationObservationRelay()
    relay.publish(proof)
    body = UpgradeColdContinuationRequest(
        previous_recovery_id=intent.recovery_id,
        previous_intent_sha256=intent.sha256(),
        previous_owner_sha256=owner.sha256(),
        observation_sha256=proof.sha256(),
        request=intent.request.model_copy(update={"idempotency_key": "distinct-cold-continuation"}),
    )

    async def accept():
        async with sessions.begin() as session:
            return await service.accept_continuation(session, actor, body, relay)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("fixture continuation audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(service, "write_principal_audit", fail_audit)
        with pytest.raises(RuntimeError, match="fixture continuation audit failure"):
            await accept()
    async with sessions.begin() as session:
        predecessor = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
        assert (
            predecessor.active_slot == 1
            and predecessor.phase == "recovery_required"
            and predecessor.resolution_payload is None
        )
        assert not (await session.scalars(select(UpgradeRecoverySuccessionRow))).all()
    accepted = await asyncio.gather(accept(), accept())
    assert sorted(created for _intent, created in accepted) == [False, True]
    next_intent = accepted[0][0]
    assert next_intent == accepted[1][0] and next_intent.recovery_id != intent.recovery_id
    relay._values.clear()
    assert await accept() == (next_intent, False)
    with pytest.raises(UpgradeConflict):
        async with sessions.begin() as session:
            await service.accept_continuation(
                session, actor, body.model_copy(update={"observation_sha256": "e" * 64}), relay
            )
    async with sessions.begin() as session:
        predecessor = await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
        current = await session.get(UpgradeExpiredRecoveryRow, next_intent.recovery_id)
        original = await session.get(UpgradeOperationRow, plan.operation.job_id)
        assert predecessor.phase == "superseded" and predecessor.active_slot is None
        assert predecessor.intent_payload == intent.model_dump(
            mode="json"
        ) and predecessor.cold_claim_payload == owner.model_dump(mode="json")
        assert current.active_slot == original.active_slot == 1
        assert original_bytes == (original.payload, original.execution_plan)
        assert all(
            not fence.released
            for fence in (await session.scalars(select(UpgradeMaintenanceRow))).all()
        )
        grant = await retained_binding(session, next_intent)
        status = await read_recovery_status(session, actor, intent.recovery_id)
        assert status.phase == "superseded" and not status.active
        prior_supersession = predecessor.resolution_payload
    if unstarted:
        from hashlib import sha256

        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoverySupportProcess

        startup = UpgradeExpiredRecoverySupportProcess(
            intent=next_intent,
            runtime=owner.runtime,
            process=NodeAgentProcessIdentity(
                pid=12355,
                create_time_s=now.timestamp(),
                environment_sha256=owner.runtime.environment_sha256,
            ),
            observed_at=now,
        )
        async with sessions.begin() as session:
            await cancel_recovery(
                session,
                actor,
                next_intent.recovery_id,
                UpgradeExpiredRecoveryCancelRequest(intent_sha256=next_intent.sha256()),
            )
        now = next_intent.deadline_at + timedelta(seconds=1)
        from sqlalchemy.exc import IntegrityError

        for bad in (
            {},
            {"protocol": "cold-unstarted-supersession-v1", "unstarted_startup": {}},
            {
                "protocol": "cold-unstarted-supersession-v1",
                "unstarted_startup": {"intent": intent.model_dump(mode="json")},
            },
        ):
            async with sessions.begin() as session:
                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        failed = await session.get(
                            UpgradeExpiredRecoveryRow, next_intent.recovery_id
                        )
                        failed.phase, failed.active_slot, failed.resolution_payload = (
                            "superseded",
                            None,
                            bad,
                        )
                        await session.flush()
        async with sessions.begin() as session:
            metadata = await context.read_cold_predecessor_context(
                session, intent.recovery_id, read, plan.operation.node
            )
            assert metadata.unstarted_intent == next_intent and metadata.owner == owner
        fresh = UpgradeColdContinuationObservation.model_validate(
            proof.model_copy(
                update={
                    "binding": proof.binding.model_copy(update={"observed_at": now}),
                    "observed_at": now,
                    "local": None,
                    "peer": proof.peer.model_copy(update={"observed_at": now}),
                    "native_data": proof.native_data.model_copy(update={"observed_at": now}),
                    "unstarted_startup": startup,
                }
            ).model_dump(mode="json")
        )
        relay.publish(fresh)
        followup = body.model_copy(
            update={
                "observation_sha256": fresh.sha256(),
                "request": body.request.model_copy(
                    update={"idempotency_key": sha256(b"unstarted-custody-next-intent").hexdigest()}
                ),
            }
        )

        async def accept_followup():
            async with sessions.begin() as session:
                return await service.accept_continuation(session, actor, followup, relay)

        followup_results = await asyncio.gather(accept_followup(), accept_followup())
        assert sum(created for _, created in followup_results) == 1
        recovered_intent = followup_results[0][0]
        assert all(actual == recovered_intent for actual, _ in followup_results)
        async with sessions.begin() as session:
            assert (
                await session.get(UpgradeExpiredRecoveryRow, intent.recovery_id)
            ).resolution_payload == prior_supersession
            failed = await session.get(UpgradeExpiredRecoveryRow, next_intent.recovery_id)
            assert failed.phase == "superseded" and failed.active_slot is None
            assert failed.cold_claim_payload is None
            final_grant = await retained_binding(session, recovered_intent)
            assert final_grant.unstarted_startup == startup
            replay, created = await service.accept_continuation(
                session, actor, followup, relay_module.ColdContinuationObservationRelay()
            )
            assert replay == recovered_intent and not created
        return
    next_owner = UpgradeExpiredRecoveryColdClaim(
        intent=next_intent,
        runtime=owner.runtime,
        process=NodeAgentProcessIdentity(
            pid=12350,
            create_time_s=now.timestamp(),
            environment_sha256=owner.runtime.environment_sha256,
        ),
        kernel_boot_identity=owner.kernel_boot_identity,
        original_claim=(
            binding.post_handback.released_claim
            if binding.post_handback is not None
            else owner.original_claim
        ),
        original_native=owner.original_native,
        previous_guard=guard,
        last_node=node,
        supervision="fixture",
        retired_supervisor=owner.retired_supervisor,
        claimed_at=now,
        continuation=grant.binding,
    )
    async with sessions.begin() as session:
        with pytest.raises((UpgradeConflict, ValidationError)):
            await cold.accept_cold_claim(
                session, next_owner.model_copy(update={"continuation": None}), plan.operation.node
            )
        actual, created = await cold.accept_cold_claim(session, next_owner, plan.operation.node)
        assert created and actual == next_owner
        assert (
            await cold.verify_cold_owner(
                session,
                next_intent.recovery_id,
                next_owner.sha256(),
                next_owner.process,
                plan.operation.node,
            )
            == next_owner
        )
    if inherited:
        from coire_core.models.upgrade_recovery import (
            UpgradeExpiredRecoveryColdSourceIntent,
            UpgradeExpiredRecoveryColdSourceLaunch,
            UpgradeExpiredRecoveryColdSourceProcess,
            UpgradeExpiredRecoveryColdSourceReceipt,
        )

        projection = UpgradeExpiredRecoveryColdSourceIntent(
            owner=next_owner,
            before_pointer=next_owner.original_native.source.runtime,
            before_bootstrap_runtime=next_owner.previous_guard.bootstrap_runtime,
            source_selection=next_owner.continuation.retained_node.selected,
            projected_native=next_owner.original_native.model_copy(
                update={
                    "phase": "prepared"
                    if next_owner.original_native.activation_started_at is None
                    else "source_selected",
                    "updated_at": now,
                }
            ),
            prepared_at=now,
        )
        receipt = UpgradeExpiredRecoveryColdSourceReceipt(projection=projection, observed_at=now)
        next_source = UpgradeExpiredRecoveryColdSourceProcess(
            launch=UpgradeExpiredRecoveryColdSourceLaunch(
                source=receipt, attempt=0, started_at=now, retained_process=source.process
            ),
            process=source.process,
            observed_at=now,
        )
        async with sessions.begin() as session:
            await cancel_recovery(
                session,
                actor,
                next_intent.recovery_id,
                UpgradeExpiredRecoveryCancelRequest(intent_sha256=next_intent.sha256()),
            )
        now = next_intent.deadline_at + timedelta(seconds=1)
        async with sessions.begin() as session:
            metadata = await context.read_cold_predecessor_context(
                session,
                next_intent.recovery_id,
                UpgradeColdContinuationRead(
                    previous_intent_sha256=next_intent.sha256(),
                    previous_owner_sha256=next_owner.sha256(),
                ),
                plan.operation.node,
            )
        held = next_intent.local_recovery_maintenance
        fresh_binding = UpgradeColdContinuationBinding(
            previous_recovery_id=next_intent.recovery_id,
            previous_intent_sha256=next_intent.sha256(),
            previous_owner_sha256=next_owner.sha256(),
            previous_source_sha256=next_source.sha256(),
            previous_deadline_at=next_intent.deadline_at,
            previous_kernel_boot_identity=next_owner.kernel_boot_identity,
            retired_supervisor_sha256=next_owner.retired_supervisor.sha256(),
            original_claim=next_owner.original_claim,
            original_native=next_owner.original_native,
            projected_native=projection.projected_native,
            previous_guard=next_owner.previous_guard.model_copy(
                update={"process": next_owner.process, "bootstrap_runtime": next_owner.runtime}
            ),
            retained_node=next_owner.last_node,
            core_data_baseline_sha256=metadata.data_baseline.sha256(),
            native_data_baseline_sha256=metadata.native_baseline.sha256(),
            previous_local_recovery_maintenance=held,
            untransferred_archive_custodian=owner.intent.recovery_id,
            untransferred_archive_sha256=previous_archive.sha256(),
            untransferred_history_sha256=previous_archive.archive.history_sha256,
            observed_at=now,
        )
        fresh = UpgradeColdContinuationObservation(
            binding=fresh_binding,
            previous_owner=next_owner,
            previous_source=next_source,
            observer_release=proof.observer_release,
            observer_runtime=proof.observer_runtime,
            observer=proof.observer,
            native_baseline=metadata.native_baseline,
            native_data=metadata.native_baseline.model_copy(update={"observed_at": now}),
            scopes=proof.scopes,
            interrupted_scope_sha256s=proof.interrupted_scope_sha256s,
            local=proof.local.model_copy(
                update={"job_id": held.job_id, "generation": held.generation, "observed_at": now}
            ),
            peer=proof.peer.model_copy(update={"observed_at": now}),
            observed_at=now,
        )
        relay.publish(fresh)
        followup = UpgradeColdContinuationRequest(
            previous_recovery_id=next_intent.recovery_id,
            previous_intent_sha256=next_intent.sha256(),
            previous_owner_sha256=next_owner.sha256(),
            observation_sha256=fresh.sha256(),
            request=body.request.model_copy(
                update={"idempotency_key": "inherited-local-recovery-fence"}
            ),
        )

        async def accept_followup():
            async with sessions.begin() as session:
                return await service.accept_continuation(session, actor, followup, relay)

        followup_results = await asyncio.gather(accept_followup(), accept_followup())
        assert sum(created for _, created in followup_results) == 1
        final = followup_results[0][0]
        assert (
            final.inherited_local_maintenance == held and final.local_recovery_maintenance is None
        )
        async with sessions.begin() as session:
            assert (await retained_binding(session, final)).binding == fresh_binding
            assert await service.accept_continuation(
                session, actor, followup, relay_module.ColdContinuationObservationRelay()
            ) == (final, False)


def completed_handback_after_archive(source, archive, at, data_before, native_before, local, peer):
    """Synthetic complete native history for real Core transaction tests."""
    from uuid import NAMESPACE_URL, uuid4, uuid5

    from coire_core.models.sharding import ProbeOutcome
    from coire_core.models.upgrade_recovery import (
        UpgradeExpiredRecoveryColdHandbackHelper,
        UpgradeExpiredRecoveryColdHandbackIntent,
        UpgradeExpiredRecoveryColdHandbackReceipt,
        UpgradeExpiredRecoveryColdPairQualification,
        UpgradeExpiredRecoveryColdPairResult,
        UpgradeExpiredRecoveryColdVerification,
        UpgradeExpiredRecoveryColdVerificationRequest,
    )

    owner = source.launch.source.projection.owner
    smoke, failed = failed_pair_after_archive(source, archive, at)
    results = []
    for index in range(3):
        dispatch = failed.dispatch.model_copy(
            update={
                "index": index,
                "command": failed.dispatch.command.model_copy(
                    update={
                        "command_id": uuid5(
                            NAMESPACE_URL,
                            f"coire-expired-recovery-pair/{owner.intent.recovery_id}/{owner.intent.sha256()}/{index}",
                        )
                    }
                ),
            }
        )
        result = UpgradeExpiredRecoveryColdPairResult(
            dispatch=dispatch,
            observation=failed.observation.model_copy(
                update={
                    "id": uuid4(),
                    "outcome": ProbeOutcome.SUCCEEDED,
                    "reason": None,
                    "runtime_sha256_a": owner.original_native.source.runtime.environment_sha256,
                    "runtime_sha256_b": owner.intent.original_plan.peer_runtime_sha256,
                    "bandwidth_bytes_per_second": 1024,
                    "latency_ms": 1.0,
                }
            ),
        )
        results.append(result)
    qualified = UpgradeExpiredRecoveryColdPairQualification(
        results=tuple(results), local=local, peer=peer, observed_at=at
    )
    request = UpgradeExpiredRecoveryColdVerificationRequest(
        smoke=smoke,
        archive=archive,
        pair=qualified,
        native_after=native_before.model_copy(update={"observed_at": at}),
    )
    verification = UpgradeExpiredRecoveryColdVerification(
        request=request,
        data_before=data_before,
        data_after=data_before.model_copy(update={"observed_at": at}),
        native_before=native_before,
        local=local,
        peer=peer,
        verified_at=at,
    )
    intent = UpgradeExpiredRecoveryColdHandbackIntent(
        verification=verification,
        before_selection=source.launch.source.projection.source_selection,
        before_runtime=owner.runtime,
        before_accepted_claim=owner.original_claim,
        released_claim=owner.original_claim.model_copy(
            update={"state": "released", "updated_at": at}
        ),
        started_at=at,
    )
    helper = UpgradeExpiredRecoveryColdHandbackHelper(
        handback=intent,
        process=NodeAgentProcessIdentity(
            pid=12351,
            create_time_s=at.timestamp(),
            environment_sha256=owner.original_native.source.runtime.environment_sha256,
        ),
        observed_at=at,
    )
    handback = UpgradeExpiredRecoveryColdHandbackReceipt(
        helper=helper,
        status=NativeGuardStatus(
            node=owner.original_claim.node,
            bootstrap_runtime=owner.original_native.source.runtime,
            process=helper.process,
            selected=source.launch.source.projection.source_selection,
            ready=True,
            observed_at=at,
        ),
        node=NativeNodeProcess(
            selected=source.launch.source.projection.source_selection,
            process=source.process,
            recorded_at=source.observed_at,
        ),
        observed_at=at,
    )
    return smoke, qualified, handback
