"""Fixed installed observation of a retired cold owner and its exact Source child."""

import logging
import os
import platform
import sys
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psutil
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
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
from coire_node.expired_recovery_cold_owner import observe_fixture_supervisor
from coire_node.expired_recovery_continuation_history import (
    retained_archive_boundary,
    retained_qualification_boundary,
)
from coire_node.expired_recovery_native_data import capture_native_data
from coire_node.expired_recovery_post_handback import retained_post_handback
from coire_node.native_platform import is_darwin
from coire_node.recovery_identity import recovery_kernel_boot_identity, verify_retired_identity
from coire_node.upgrade_guard import (
    NativeGuardTransport,
    _owned_native_process,
    _owned_node_process,
)
from coire_node.upgrade_inventory import native_process_entries
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.cold_continuation_observer")
logger = logging.getLogger(__name__)


@tracer.start_as_current_span("coire.node.cold_continuation_observe", record_exception=False)
def observe_continuation(
    settings: Settings, body: UpgradeColdContinuationObserverInput, control: NativeGuardTransport
) -> UpgradeColdContinuationObservation:
    # Metadata only. This deadline cannot authorize recovery effects or a claim.
    io_deadline = datetime.now(UTC) + timedelta(seconds=15)
    envs = Path(settings.upgrade_environment_root)
    runtime = body.observer_runtime
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    actor = NodeAgentProcessIdentity(
        pid=os.getpid(),
        create_time_s=psutil.Process().create_time(),
        environment_sha256=runtime.environment_sha256,
    )
    if (
        not is_darwin(sys.platform)
        or platform.machine() != "arm64"
        or settings.node_name != "coire-edge-a"
        or platform.node().lower().split(".", 1)[0] != settings.node_name
        or not settings.upgrades_enabled
        or Path(sys.prefix).resolve(strict=True)
        != (envs / runtime.environment_id).resolve(strict=True)
        or Path(sys.executable).resolve(strict=True) != python.resolve(strict=True)
        or _owned_native_process(actor, python, "coire_node.expired_recovery_continuation_observer")
        is None
    ):
        raise UpgradeUnavailable("Cold continuation observer is not the fixed installed module")

    def context() -> UpgradeColdContinuationContext:
        return control._read(
            f"/api/v1/internal/upgrades/{body.previous_recovery_id}/recovery-cold-continuation-context",
            UpgradeColdContinuationContext,
            io_deadline,
            core=True,
            method="POST",
            payload=body.read,
        )

    captured = context()
    intent, owner = captured.intent, captured.owner
    if (
        owner.sha256() != body.read.previous_owner_sha256
        or intent.sha256() != body.read.previous_intent_sha256
    ):
        raise UpgradeConflict("Cold continuation context changes predecessor")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    if captured.previous_handback_intent is not None:
        from coire_node.expired_recovery_partial_handback import observe_partial_handback

        return observe_partial_handback(
            settings, body, control, captured, actor, context, io_deadline
        )
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(body.previous_recovery_id), create=False)
    records = PrivateUpgradeStore(bootstrap.root.parent / "fixture-records", create=False)
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    operation = PrivateUpgradeStore(bootstrap.path("operations"), create=False)
    operation = PrivateUpgradeStore(operation.path(intent.request.original_job_id), create=False)
    guard = PrivateUpgradeStore(operation.path("guard"), create=False)
    with (
        records.ownership("fixture-supervisor.lock"),
        bootstrap.ownership("lifetime.lock")
        if captured.previous_handback is None
        else nullcontext(),
        bootstrap.ownership("recovery-transfer.lock"),
        node.ownership("maintenance.lock"),
        ExitStack() as stack,
    ):
        current = context()
        if (current.intent, current.owner, current.data_baseline, current.native_baseline) != (
            captured.intent,
            captured.owner,
            captured.data_baseline,
            captured.native_baseline,
        ):
            raise UpgradeConflict("Cold continuation predecessor changed during observation")
        if (
            run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim) != owner
            or run.read("cold.activation.json", UpgradeExpiredRecoveryColdClaim) != owner
        ):
            raise UpgradeConflict("Cold continuation has no exact native predecessor")
        selection = run.read("cold.source.receipt.json", UpgradeExpiredRecoveryColdSourceReceipt)
        source = run.read("cold.source.process.0.json", UpgradeExpiredRecoveryColdSourceProcess)
        if (
            selection is None
            or source is None
            or source.launch.source != selection
            or selection.projection.owner != owner
        ):
            raise UpgradeConflict("Cold continuation Source launch provenance differs")
        post = retained_post_handback(run, source, captured.previous_handback)
        retired = [
            owner.process,
            owner.original_native.source_process,
            owner.previous_guard.process,
        ]
        if owner.continuation is None:
            retired.append(owner.last_node.process)
        elif owner.last_node.process != source.process:
            raise UpgradeConflict("Cold continuation retained Source differs from predecessor")
        for identity in retired:
            verify_retired_identity(identity)
        origin = fixture_origin(settings, owner)
        if (
            owner.retired_supervisor is None
            or observe_fixture_supervisor(
                settings, origin.previous_guard, origin.last_node, origin.original_claim
            )
            != owner.retired_supervisor
        ):
            raise UpgradeConflict("Cold continuation fixture restart provenance differs")
        verify_retired_identity(owner.retired_supervisor)
        if recovery_kernel_boot_identity(io_deadline) != owner.kernel_boot_identity:
            raise UpgradeUnavailable("Cold continuation kernel changed")
        verify_prepared_runtime(envs, envs.parent / "runtimes", owner.runtime)
        source_python = verify_prepared_runtime(
            envs, envs.parent / "runtimes", owner.original_native.source.runtime
        )
        child = _owned_node_process(source.process, source_python)
        if child is None or child.cmdline() != [str(source_python), "-I", "-B", "-m", "coire_node"]:
            raise UpgradeUnavailable(
                "Cold continuation Source is absent or has another physical identity"
            )
        if post is not None:
            from coire_node.expired_recovery_cold_handback import source_writer_lock_held

            handback, supervisor, _closure = post
            verify_retired_identity(supervisor.process)
            keeper = _owned_native_process(
                handback.helper.process, source_python, "coire_node.upgrade_guard"
            )
            if (
                keeper is None
                or keeper.cmdline()
                != [str(source_python), "-I", "-B", "-m", "coire_node.upgrade_guard"]
                or not source_writer_lock_held(bootstrap)
            ):
                raise UpgradeUnavailable(
                    "Post-handback continuation has no recorded live Source writer"
                )

        def preimages() -> tuple[
            NativeUpgradeClaim | None,
            NativeRecoveryIntent | None,
            NativeGuardStatus | None,
            NativeNodeProcess | None,
            PreparedNativeRuntime | None,
            NativeRuntimeSelection | None,
            UpgradeMaintenance | None,
            str,
        ]:
            current_guard = bootstrap.read("status.json", NativeGuardStatus)
            if post is not None:
                if (
                    current_guard is None
                    or not 0 <= (datetime.now(UTC) - current_guard.observed_at).total_seconds() <= 5
                ):
                    raise UpgradeUnavailable("Post-handback Source guard observation is stale")
                current_guard = current_guard.model_copy(
                    update={"observed_at": post[0].status.observed_at}
                )
            return (
                bootstrap.read("claim.json", NativeUpgradeClaim),
                guard.read("intent.json", NativeRecoveryIntent),
                current_guard,
                node.read("node-process.json", NativeNodeProcess),
                bootstrap.read("runtime.json", PreparedNativeRuntime),
                bootstrap.read("selection.json", NativeRuntimeSelection),
                node.read("maintenance.json", UpgradeMaintenance),
                str((envs / "current").resolve(strict=True)),
            )

        before = preimages()
        claim, native, status, last, helper_runtime, _baseline_selection, maintenance, pointer = (
            before
        )
        if (
            claim
            != (
                post[0].helper.handback.released_claim if post is not None else owner.original_claim
            )
            or native != selection.projection.projected_native
            or status is None
            or status.process != (post[0].helper.process if post is not None else owner.process)
            or status.bootstrap_runtime
            != (owner.original_native.source.runtime if post is not None else owner.runtime)
            or status.selected != selection.projection.source_selection
            or last is None
            or last.process != source.process
            or last.selected != selection.projection.source_selection
            or helper_runtime
            != (owner.original_native.source.runtime if post is not None else owner.runtime)
            or maintenance is None
            or (maintenance.state == "released" and post is None)
            or maintenance.model_dump(exclude={"state", "updated_at"})
            != intent.local_maintenance().model_dump(exclude={"state", "updated_at"})
            or not (envs / "current").is_symlink()
            or pointer
            != str(
                (envs / owner.original_native.source.runtime.environment_id).resolve(strict=True)
            )
        ):
            raise UpgradeConflict("Cold continuation native preimages differ")
        allowed = {
            (actor.pid, actor.create_time_s),
            (source.process.pid, source.process.create_time_s),
        }
        if post is not None:
            allowed.add((post[0].helper.process.pid, post[0].helper.process.create_time_s))
        if any(
            (entry.process_id, entry.process_created_at) not in allowed
            for entry in native_process_entries("cold-continuation", (str(envs.parent),))
        ):
            raise UpgradeUnavailable("Cold continuation private scope has another physical owner")
        archived, smoke, pair_results = retained_archive_boundary(
            run, source, captured.previous_smoke_command
        )
        historical_qualification = retained_qualification_boundary(
            run, smoke, pair_results or (), archived
        )
        pending_archive = None
        if archived is None and intent.local_maintenance() != intent.original_plan.maintenance:
            from coire_node.expired_recovery_archive_custody import pending_archive_custody

            pending_archive = pending_archive_custody(settings, owner)
        collective_root = PrivateUpgradeStore(node.path("collectives"), create=False)
        original_history = collective_root.path(intent.request.original_job_id)
        if archived is not None:
            if original_history.exists() or original_history.is_symlink():
                raise UpgradeConflict("Cold continuation archive has two namespace owners")
            history = PrivateUpgradeStore(
                run.path("collective-history") / intent.request.original_job_id, create=False
            )
        else:
            history = PrivateUpgradeStore(
                runs.path(pending_archive[0])
                / "collective-history"
                / intent.request.original_job_id
                if pending_archive is not None
                else original_history,
                create=False,
            )
        for stage in sorted(history.root.iterdir()):
            for slot in sorted(PrivateUpgradeStore(stage, create=False).root.iterdir()):
                stack.enter_context(
                    PrivateUpgradeStore(slot, create=False).ownership("collective.lock")
                )
        scopes, interrupted = retained_dispatches(history)
        history_digest = history_sha256(history)
        if archived is not None and (
            archived.archive.scopes != scopes
            or archived.archive.interrupted_scope_sha256s != interrupted
            or archived.archive.history_sha256 != history_digest
        ):
            raise UpgradeConflict("Cold continuation preserved archive bytes differ")
        local = (
            control.local_inventory(intent.local_inventory_plan(), deadline_at=io_deadline)
            if post is None
            else None
        )
        peer = control.peer_inventory(intent.original_plan, deadline_at=io_deadline)
        verify_retained_collective_inventory(
            intent.original_plan,
            intent.request.original_scope_sha256s,
            scopes,
            () if archived is not None or pending_archive is not None else interrupted,
            local,
            peer,
            defer_local=post is not None,
        )
        native_data = capture_native_data(settings, intent.original_plan, deadline_at=io_deadline)
        final = context()
        unstarted = None
        if captured.unstarted_intent is not None:
            from coire_node.expired_recovery_unstarted_lineage import observe_unstarted_custodian

            unstarted = observe_unstarted_custodian(settings, captured.unstarted_intent)
        if (
            before != preimages()
            or history_digest != history_sha256(history)
            or final.owner != owner
            or final.native_baseline != captured.native_baseline
            or final.data_baseline != captured.data_baseline
            or final.previous_smoke_command != captured.previous_smoke_command
            or final.previous_handback != captured.previous_handback
            or final.unstarted_intent != captured.unstarted_intent
            or retained_post_handback(run, source, final.previous_handback) != post
            or retained_archive_boundary(run, source, final.previous_smoke_command)
            != (archived, smoke, pair_results)
            or retained_qualification_boundary(run, smoke, pair_results or (), archived)
            != historical_qualification
        ):
            raise UpgradeConflict("Cold continuation metadata changed while observed")
        verify_retired_identity(owner.process)
        if post is not None:
            from coire_node.expired_recovery_cold_handback import source_writer_lock_held

            verify_retired_identity(post[1].process)
            if _owned_native_process(
                post[0].helper.process, source_python, "coire_node.upgrade_guard"
            ) is None or not source_writer_lock_held(bootstrap):
                raise UpgradeUnavailable(
                    "Cold continuation Source writer retired during observation"
                )
        if _owned_node_process(source.process, source_python) is None:
            raise UpgradeUnavailable("Cold continuation Source retired during observation")
        now = datetime.now(UTC)
        if now >= io_deadline:
            raise UpgradeUnavailable("Cold continuation metadata observation exhausted its bound")
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
            previous_archive_sha256=archived.sha256() if archived is not None else None,
            previous_history_sha256=history_digest if archived is not None else None,
            previous_smoke_sha256=smoke.sha256() if smoke is not None else None,
            previous_pair_result_sha256s=(
                tuple(result.sha256() for result in pair_results)
                if pair_results is not None
                else None
            ),
            previous_pair_qualification_sha256=historical_qualification[0],
            previous_verification_request_sha256=historical_qualification[1],
            post_handback=(
                UpgradeColdPostHandbackBinding(
                    handback_sha256=post[0].sha256(),
                    supervision_sha256=post[1].sha256(),
                    closure_intent_sha256=post[2].sha256(),
                    released_claim=claim,
                    released_maintenance=maintenance,
                    source_helper=status,
                    retired_supervisor=post[1].process,
                )
                if post is not None
                else None
            ),
            previous_local_recovery_maintenance=intent.local_maintenance()
            if post is None and intent.local_maintenance() != intent.original_plan.maintenance
            else None,
            untransferred_archive_custodian=pending_archive[0]
            if pending_archive is not None
            else None,
            untransferred_archive_sha256=pending_archive[1]
            if pending_archive is not None
            else None,
            untransferred_history_sha256=pending_archive[2]
            if pending_archive is not None
            else None,
            observed_at=now,
        )
        proof = UpgradeColdContinuationObservation(
            binding=binding,
            previous_owner=owner,
            previous_source=source,
            previous_archive=archived,
            previous_smoke=smoke,
            previous_pair_results=pair_results,
            observer_release=body.observer_release,
            observer_runtime=runtime,
            observer=actor,
            native_baseline=captured.native_baseline,
            native_data=native_data,
            scopes=scopes,
            interrupted_scope_sha256s=interrupted,
            local=local,
            peer=peer,
            observed_at=now,
            unstarted_startup=unstarted,
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
            raise UpgradeConflict("Cold continuation observation acknowledgment differs")
        return proof


def main() -> None:
    from coire_core.logging import StructuredJSONFormatter
    from coire_node.keychain import NODE_TOKEN_ITEM, read_item
    from coire_node.otel import configure_node_telemetry

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    settings = Settings()
    if not settings.node_token.get_secret_value():
        settings.node_token = read_item(NODE_TOKEN_ITEM)
    configure_node_telemetry(
        "cold-continuation-observer",
        settings.otlp_endpoint,
        diagnostics_enabled=settings.diagnostics_enabled,
    )
    try:
        raw = sys.stdin.buffer.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise UpgradeUnavailable("Cold continuation observer input exceeds bound")
        body = UpgradeColdContinuationObserverInput.model_validate_json(raw)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            proof = observe_continuation(settings, body, NativeGuardTransport(settings, client))
        print(proof.model_dump_json())
    except CoireError as error:
        logger.error("cold continuation observation refused", extra={"reason": error.detail})
        raise SystemExit(1) from None
    except (OSError, ValueError, psutil.Error):
        logger.error("cold continuation observation could not be verified")
        raise SystemExit(1) from None
    finally:
        provider = metrics.get_meter_provider()
        if isinstance(provider, MeterProvider):
            provider.shutdown(timeout_millis=1000)


if __name__ == "__main__":
    main()
