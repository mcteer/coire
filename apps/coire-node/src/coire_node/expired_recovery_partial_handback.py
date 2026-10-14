"""Positive metadata capture of a ready Source whose old handback was unreceipted."""

import hashlib
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdHandbackHelper,
    UpgradeExpiredRecoveryColdHandbackIntent,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryColdSourceReceipt,
)
from coire_core.models.upgrade_recovery_cold_binding import (
    UpgradeColdContinuationBinding,
    UpgradeColdPostHandbackBinding,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationContext,
    UpgradeColdContinuationObservation,
    UpgradeColdContinuationObserverInput,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeRuntimeSelection,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    PreparedNativeRuntime,
    UpgradeMaintenance,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_core.upgrade_recovery_inventory import verify_retained_collective_inventory
from coire_node.expired_recovery_cold_archive import history_sha256, retained_dispatches
from coire_node.expired_recovery_cold_continuation import fixture_origin
from coire_node.expired_recovery_cold_handback import source_writer_lock_held
from coire_node.expired_recovery_cold_owner import observe_fixture_supervisor
from coire_node.expired_recovery_continuation_history import (
    retained_archive_boundary,
    retained_qualification_boundary,
)
from coire_node.expired_recovery_native_data import capture_native_data
from coire_node.recovery_identity import recovery_kernel_boot_identity, verify_retired_identity
from coire_node.upgrade_guard import (
    NativeGuardTransport,
    _owned_native_process,
    _owned_node_process,
)
from coire_node.upgrade_inventory import native_process_entries
from coire_node.upgrade_runtime import verify_prepared_runtime


def retained_unreceipted_helper(
    run: PrivateUpgradeStore,
    source: UpgradeExpiredRecoveryColdSourceProcess,
    intent: UpgradeExpiredRecoveryColdHandbackIntent,
) -> UpgradeExpiredRecoveryColdHandbackHelper:
    helper = run.read("cold.handback.helper.json", UpgradeExpiredRecoveryColdHandbackHelper)
    if (
        helper is None
        or helper.handback != intent
        or helper.handback.verification.request.smoke.smoke.source != source
        or run.read("cold.handback.intent.json", UpgradeExpiredRecoveryColdHandbackIntent) != intent
    ):
        raise UpgradeConflict("Unreceipted handback has no exact retained helper provenance")
    if any(
        run.path(key).exists() or run.path(key).is_symlink()
        for key in (
            "cold.handback.receipt.json",
            "cold.supervision.intent.json",
            "cold.supervision.receipt.json",
            "cold.closure.intent.json",
            "cold.closure.receipt.json",
            "cold.resolution.json",
        )
    ):
        raise UpgradeUnavailable("Unreceipted Source has an uncertain later boundary")
    return helper


def observe_partial_handback(
    settings: Settings,
    body: UpgradeColdContinuationObserverInput,
    control: NativeGuardTransport,
    captured: UpgradeColdContinuationContext,
    actor: NodeAgentProcessIdentity,
    context: Callable[[], UpgradeColdContinuationContext],
    io_deadline: datetime,
) -> UpgradeColdContinuationObservation:
    owner, intent = captured.owner, captured.intent
    old_handback = captured.previous_handback_intent
    if old_handback is None or captured.previous_handback is not None:
        raise UpgradeConflict("Partial handback metadata context differs")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    run = PrivateUpgradeStore(bootstrap.path("expired-recovery") / intent.recovery_id, create=False)
    records = PrivateUpgradeStore(bootstrap.root.parent / "fixture-records", create=False)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    guard = PrivateUpgradeStore(
        bootstrap.path("operations") / intent.request.original_job_id / "guard", create=False
    )
    custody = None
    if captured.unstarted_intent is not None:
        from coire_node.expired_recovery_unstarted_lineage import observe_retired_keeper_custodian

        custody = observe_retired_keeper_custodian(settings, captured.unstarted_intent)
    envs = Path(settings.upgrade_environment_root)
    with (
        records.ownership("fixture-supervisor.lock"),
        bootstrap.ownership("recovery-transfer.lock"),
        node.ownership("maintenance.lock"),
        bootstrap.ownership("lifetime.lock") if custody is not None else nullcontext(),
    ):
        if (
            run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim) != owner
            or run.read("cold.activation.json", UpgradeExpiredRecoveryColdClaim) != owner
        ):
            raise UpgradeConflict("Partial Source changes retained predecessor ownership")
        selection = run.read("cold.source.receipt.json", UpgradeExpiredRecoveryColdSourceReceipt)
        source = run.read("cold.source.process.0.json", UpgradeExpiredRecoveryColdSourceProcess)
        if (
            selection is None
            or source is None
            or source.launch.source != selection
            or selection.projection.owner != owner
        ):
            raise UpgradeConflict("Partial Source launch provenance differs")
        helper = retained_unreceipted_helper(run, source, old_handback)
        if custody is not None and (
            custody[1].grant.binding.previous_owner_sha256 != owner.sha256()
            or custody[1].grant.binding.post_handback is None
            or custody[1].grant.binding.post_handback.helper_sha256 != helper.sha256()
        ):
            raise UpgradeConflict("Keeper preclaim custody changes original helper")
        origin = fixture_origin(settings, owner)
        if (
            owner.retired_supervisor is None
            or observe_fixture_supervisor(
                settings, origin.previous_guard, origin.last_node, origin.original_claim
            )
            != owner.retired_supervisor
        ):
            raise UpgradeConflict("Partial Source fixture exclusion differs")
        for identity in (
            owner.process,
            owner.original_native.source_process,
            owner.previous_guard.process,
            owner.retired_supervisor,
        ):
            verify_retired_identity(identity)
        if owner.continuation is None:
            verify_retired_identity(owner.last_node.process)
        elif owner.last_node.process != source.process:
            raise UpgradeConflict("Partial Source retained node differs from predecessor")
        if recovery_kernel_boot_identity(io_deadline) != owner.kernel_boot_identity:
            raise UpgradeUnavailable("Partial Source kernel changed")
        python = verify_prepared_runtime(
            envs, envs.parent / "runtimes", owner.original_native.source.runtime
        )

        def preimages() -> tuple[
            NativeGuardStatus, NativeNodeProcess, NativeUpgradeClaim, UpgradeMaintenance
        ]:
            status = bootstrap.read("status.json", NativeGuardStatus)
            last = node.read("node-process.json", NativeNodeProcess)
            claim = bootstrap.read("claim.json", NativeUpgradeClaim)
            native = guard.read("intent.json", NativeRecoveryIntent)
            runtime = bootstrap.read("runtime.json", PreparedNativeRuntime)
            baseline = bootstrap.read("selection.json", NativeRuntimeSelection)
            maintenance = node.read("maintenance.json", UpgradeMaintenance)
            keeper = _owned_native_process(helper.process, python, "coire_node.upgrade_guard")
            child = _owned_node_process(source.process, python)
            if (
                (keeper is None if custody is None else keeper is not None)
                or child is None
                or (
                    custody is None
                    and keeper is not None
                    and keeper.cmdline()
                    != [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
                )
                or child.cmdline() != [str(python), "-I", "-B", "-m", "coire_node"]
                or (custody is None and not source_writer_lock_held(bootstrap))
                or status is None
                or not status.ready
                or status.process != helper.process
                or status.bootstrap_runtime != owner.original_native.source.runtime
                or status.selected != selection.projection.source_selection
                or (
                    custody is None
                    and not 0 <= (datetime.now(UTC) - status.observed_at).total_seconds() <= 5
                )
                or last is None
                or last.process != source.process
                or last.selected != selection.projection.source_selection
                or claim != old_handback.released_claim
                or native != selection.projection.projected_native
                or runtime != owner.original_native.source.runtime
                or baseline != selection.projection.source_selection
                or maintenance is None
                or maintenance.state == "released"
                or not maintenance.matches_plan(intent.local_inventory_plan())
                or not (envs / "current").is_symlink()
                or (envs / "current").resolve(strict=True)
                != (envs / runtime.environment_id).resolve(strict=True)
            ):
                raise UpgradeConflict("Partial Source ready writer/native preimages differ")
            return (
                status,
                last,
                claim,
                maintenance,
            )

        before = preimages()
        allowed = {
            (actor.pid, actor.create_time_s),
            (source.process.pid, source.process.create_time_s),
            (helper.process.pid, helper.process.create_time_s),
        }
        if any(
            (p.process_id, p.process_created_at) not in allowed
            for p in native_process_entries("partial-handback", (str(envs.parent),))
        ):
            raise UpgradeUnavailable("Partial Source has unknown physical owners")
        archive, smoke, results = retained_archive_boundary(
            run, source, captured.previous_smoke_command
        )
        if archive is None or smoke is None or results is None:
            raise UpgradeUnavailable("Partial Source has no completed qualification history")
        qualification = retained_qualification_boundary(run, smoke, results, archive)
        if intent.request.original_scope_sha256s:
            history = PrivateUpgradeStore(
                run.path("collective-history") / intent.request.original_job_id, create=False
            )
            scopes, interrupted = retained_dispatches(history)
            digest = history_sha256(history)
        else:
            for path in (
                node.path("collectives") / intent.request.original_job_id,
                run.path("collective-history") / intent.request.original_job_id,
            ):
                if path.exists() or path.is_symlink():
                    raise UpgradeConflict("Partial no-dispatch namespace appeared")
            scopes, interrupted = (), ()
            digest = hashlib.sha256(b"coire-cold-no-original-dispatch-v1").hexdigest()
        if archive.archive.history_sha256 != digest or archive.archive.scopes != scopes:
            raise UpgradeConflict("Partial Source original archive changed")
        local = control.local_inventory(intent.local_inventory_plan(), deadline_at=io_deadline)
        peer = control.peer_inventory(intent.original_plan, deadline_at=io_deadline)
        verify_retained_collective_inventory(
            intent.original_plan, intent.request.original_scope_sha256s, scopes, (), local, peer
        )
        data = capture_native_data(settings, intent.original_plan, deadline_at=io_deadline)
        final = context()
        after = preimages()
        if (
            (
                final.owner,
                final.data_baseline,
                final.native_baseline,
                final.previous_handback_intent,
                final.previous_handback,
                final.previous_smoke_command,
            )
            != (
                captured.owner,
                captured.data_baseline,
                captured.native_baseline,
                old_handback,
                None,
                captured.previous_smoke_command,
            )
            or after[1:] != before[1:]
            or after[0].model_dump(exclude={"observed_at"})
            != before[0].model_dump(exclude={"observed_at"})
            or retained_unreceipted_helper(run, source, old_handback) != helper
        ):
            raise UpgradeConflict("Partial Source metadata changed while observed")
        status, last, claim, maintenance = before
        now = datetime.now(UTC)
        if now >= io_deadline:
            raise UpgradeUnavailable("Partial Source metadata read exhausted its bound")
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
            projected_native=selection.projection.projected_native,
            previous_guard=status,
            retained_node=last,
            core_data_baseline_sha256=captured.data_baseline.sha256(),
            native_data_baseline_sha256=captured.native_baseline.sha256(),
            previous_archive_sha256=archive.sha256(),
            previous_history_sha256=digest,
            previous_smoke_sha256=smoke.sha256(),
            previous_pair_result_sha256s=tuple(result.sha256() for result in results),
            previous_pair_qualification_sha256=qualification[0],
            previous_verification_request_sha256=qualification[1],
            post_handback=UpgradeColdPostHandbackBinding(
                stage="keeper_retired" if custody is not None else "ready_unreceipted",
                keeper_custodian_recovery_id=custody[0].intent.recovery_id
                if custody is not None
                else None,
                keeper_retirement_sha256=custody[1].sha256() if custody is not None else None,
                handback_sha256=old_handback.sha256(),
                helper_sha256=helper.sha256(),
                released_claim=claim,
                released_maintenance=maintenance,
                source_helper=status,
            ),
            observed_at=now,
        )
        proof = UpgradeColdContinuationObservation(
            binding=binding,
            previous_owner=owner,
            previous_source=source,
            previous_archive=archive,
            previous_smoke=smoke,
            previous_pair_results=results,
            previous_unreceipted_helper=helper,
            unstarted_startup=custody[0] if custody is not None else None,
            unstarted_keeper_retirement=custody[1] if custody is not None else None,
            observer_release=body.observer_release,
            observer_runtime=body.observer_runtime,
            observer=actor,
            native_baseline=captured.native_baseline,
            native_data=data,
            scopes=scopes,
            interrupted_scope_sha256s=interrupted,
            local=local,
            peer=peer,
            observed_at=now,
        )
        run.write(f"cold.continuation.observation.{proof.sha256()}.json", proof, replace=False)
        observed = control._read(
            f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-cold-continuation-observation",
            UpgradeColdContinuationObservation,
            io_deadline,
            core=True,
            method="POST",
            payload=proof,
        )
        if observed != proof:
            raise UpgradeConflict("Partial Source metadata acknowledgment differs")
        return proof
