"""Fixed Source helper handback under separate audited authority and fencing."""

import fcntl
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdHandbackHelper,
    UpgradeExpiredRecoveryColdHandbackIntent,
    UpgradeExpiredRecoveryColdHandbackReceipt,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
    UpgradeExpiredRecoveryColdVerification,
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
from coire_node.expired_recovery_cold_launch import require_live_cold_source
from coire_node.expired_recovery_cold_source import stores
from coire_node.expired_recovery_support import verify_passive_support_identity
from coire_node.recovery_identity import recovery_kernel_boot_identity, verify_retired_identity
from coire_node.recovery_lease import handoff_recovery_lifetime, require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport, _owned_native_process
from coire_node.upgrade_inventory import native_process_entries
from coire_node.upgrade_runtime import verify_prepared_runtime

tracer = trace.get_tracer("coire.node.cold_recovery_handback")


def observe_source_helper(
    settings: Settings,
    handback: UpgradeExpiredRecoveryColdHandbackIntent,
) -> NodeAgentProcessIdentity | None:
    source = handback.verification.request.smoke.smoke.source
    owner = source.launch.source.projection.owner
    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(
        envs, envs.parent / "runtimes", owner.original_native.source.runtime
    )
    allowed = {
        (owner.process.pid, owner.process.create_time_s),
        (source.process.pid, source.process.create_time_s),
    }
    _, run, _ = stores(settings, owner)
    supervisor = run.read(
        "cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt
    )
    if supervisor is not None:
        from coire_node.expired_recovery_cold_supervision import verify_fixture_supervisor

        if supervisor.supervision.handback.helper.handback != handback:
            raise UpgradeConflict("Source handback supervisor has another retained intent")
        verify_fixture_supervisor(settings, supervisor)
        allowed.add((supervisor.process.pid, supervisor.process.create_time_s))
    matches = []
    for entry in native_process_entries("cold-handback", (str(envs.parent),)):
        if (entry.process_id, entry.process_created_at) in allowed:
            continue
        if (
            entry.process_id is None
            or entry.process_created_at is None
            or not handback.started_at.timestamp()
            <= entry.process_created_at
            < owner.intent.deadline_at.timestamp()
        ):
            raise UpgradeUnavailable("Source handback retains unknown physical owners")
        identity = NodeAgentProcessIdentity(
            pid=entry.process_id,
            create_time_s=entry.process_created_at,
            environment_sha256=owner.original_native.source.runtime.environment_sha256,
        )
        child = _owned_native_process(identity, python, "coire_node.upgrade_guard")
        if child is None or child.cmdline() != [
            str(python),
            "-I",
            "-B",
            "-m",
            "coire_node.upgrade_guard",
        ]:
            raise UpgradeUnavailable(
                "Source handback process does not select the exact fixed Source helper"
            )
        matches.append(identity)
    if len(matches) > 1:
        raise UpgradeUnavailable("Source handback has duplicate helper owners")
    return matches[0] if matches else None


def verify_handback_boundary(
    settings: Settings,
    handback: UpgradeExpiredRecoveryColdHandbackIntent,
    control: NativeGuardTransport,
    *,
    allow_released: bool = False,
) -> None:
    source = handback.verification.request.smoke.smoke.source
    effect = source.launch.source.projection
    owner = effect.owner
    bootstrap, run, guard = stores(settings, owner)
    require_recovery_lease(bootstrap, owner.sha256())
    if (
        run.read("cold.handback.intent.json", UpgradeExpiredRecoveryColdHandbackIntent) != handback
        or control.verify_cold_recovery_handback(handback) != handback
    ):
        raise UpgradeUnavailable("Source handback has no exact retained and live Core authority")
    if (
        verify_passive_support_identity(settings, owner.intent, owner.runtime) != owner.process
        or recovery_kernel_boot_identity(owner.intent.deadline_at) != owner.kernel_boot_identity
    ):
        raise UpgradeUnavailable("Source handback current support owner or kernel differs")
    require_live_cold_source(settings, source)
    retired = [owner.previous_guard.process, owner.original_native.source_process]
    if owner.continuation is None:
        retired.append(owner.last_node.process)
    elif owner.last_node.process != source.process:
        raise UpgradeConflict("Cold handback retained Source differs from continuation")
    for identity in retired:
        verify_retired_identity(identity)
    if bootstrap.read("claim.json", NativeUpgradeClaim) not in (
        owner.original_claim,
        handback.released_claim,
    ):
        raise UpgradeConflict("Source handback original claim has an unknown preimage")
    if bootstrap.read("runtime.json", PreparedNativeRuntime) not in (
        handback.before_runtime,
        effect.source_selection.runtime,
    ):
        raise UpgradeConflict("Source handback bootstrap runtime has an unknown preimage")
    if bootstrap.read("selection.json", NativeRuntimeSelection) not in (
        handback.before_selection,
        effect.source_selection,
    ):
        raise UpgradeConflict("Source handback baseline selection has an unknown preimage")
    operation = PrivateUpgradeStore(guard.root.parent, create=False)
    if (
        operation.read("accepted.json", NativeUpgradeClaim)
        not in (handback.before_accepted_claim, handback.released_claim)
        or guard.read("intent.json", NativeRecoveryIntent) != effect.projected_native
    ):
        raise UpgradeConflict("Source handback native operation has an unknown preimage")
    node = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    fence = node.read("maintenance.json", UpgradeMaintenance)
    valid_fence = fence is not None and fence.matches_plan(owner.intent.local_inventory_plan())
    late = owner.continuation.post_handback if owner.continuation is not None else None
    if late is not None and fence == late.released_maintenance:
        valid_fence = True
    if allow_released and fence is not None and fence.state == "released":
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdClosureIntent

        closure = run.read("cold.closure.intent.json", UpgradeExpiredRecoveryColdClosureIntent)
        valid_fence = valid_fence or (
            closure is not None
            and closure.supervision.supervision.handback.helper.handback == handback
            and fence.model_dump(exclude={"state", "updated_at"})
            == closure.before_maintenance.model_dump(exclude={"state", "updated_at"})
            and closure.started_at <= fence.updated_at < owner.intent.deadline_at
        )
    if not valid_fence:
        raise UpgradeConflict("Source handback original maintenance was released or changed")
    envs = Path(settings.upgrade_environment_root)
    if not (envs / "current").is_symlink() or (envs / "current").resolve(strict=True) != (
        envs / effect.source_selection.runtime.environment_id
    ).resolve(strict=True):
        raise UpgradeConflict("Source handback physical pointer no longer selects Source")
    observe_source_helper(settings, handback)
    require_recovery_lease(bootstrap, owner.sha256())


def source_writer_lock_held(bootstrap: PrivateUpgradeStore) -> bool:
    """Caller separately proves the unique live sealed Source helper identity."""
    descriptor = os.open(bootstrap.path("lifetime.lock"), os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(descriptor)


@tracer.start_as_current_span("coire.node.cold_recovery_handback_source")
def handback_cold_source(
    settings: Settings,
    proof: UpgradeExpiredRecoveryColdVerification,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoveryColdHandbackReceipt:
    source = proof.request.smoke.smoke.source
    effect = source.launch.source.projection
    owner = effect.owner
    bootstrap, run, guard = stores(settings, owner)
    require_recovery_lease(bootstrap, owner.sha256())
    with run.ownership("cold.handback.lock"):
        handback = run.read("cold.handback.intent.json", UpgradeExpiredRecoveryColdHandbackIntent)
        if handback is None:
            if (
                run.read("cold.verification.json", UpgradeExpiredRecoveryColdVerification) != proof
                or control.verify_cold_recovery_verification(proof) != proof
            ):
                raise UpgradeUnavailable(
                    "Source handback has no committed live Source verification"
                )
            accepted = PrivateUpgradeStore(guard.root.parent, create=False).read(
                "accepted.json", NativeUpgradeClaim
            )
            selection = bootstrap.read("selection.json", NativeRuntimeSelection)
            runtime = bootstrap.read("runtime.json", PreparedNativeRuntime)
            if accepted is None or selection is None or runtime is None:
                raise UpgradeUnavailable("Source handback preimages are incomplete")
            now = datetime.now(UTC)
            handback = UpgradeExpiredRecoveryColdHandbackIntent(
                verification=proof,
                before_selection=selection,
                before_runtime=runtime,
                before_accepted_claim=accepted,
                released_claim=(
                    owner.original_claim
                    if owner.original_claim.state == "released"
                    else owner.original_claim.model_copy(
                        update={"state": "released", "updated_at": now}
                    )
                ),
                started_at=now,
            )
            run.write("cold.handback.intent.json", handback, replace=False)
        if (
            handback.verification != proof
            or control.accept_cold_recovery_handback(handback) != handback
        ):
            raise UpgradeConflict("Source handback acknowledgment changes retained authority")
        verify_handback_boundary(settings, handback, control)
        operation = PrivateUpgradeStore(guard.root.parent, create=False)
        with bootstrap.ownership("control.lock"):
            for store, key, value, model in (
                (bootstrap, "selection.json", effect.source_selection, NativeRuntimeSelection),
                (operation, "accepted.json", handback.released_claim, NativeUpgradeClaim),
                (bootstrap, "claim.json", handback.released_claim, NativeUpgradeClaim),
                (bootstrap, "runtime.json", effect.source_selection.runtime, PreparedNativeRuntime),
            ):
                verify_handback_boundary(settings, handback, control)
                if store.read(key, model) != value:
                    store.write(key, value)
        verify_handback_boundary(settings, handback, control)
        handoff_recovery_lifetime(bootstrap, owner.sha256())
        helper = run.read("cold.handback.helper.json", UpgradeExpiredRecoveryColdHandbackHelper)
        current = observe_source_helper(settings, handback)
        if helper is None:
            if current is None:
                marker = run.read(
                    "cold.handback.launch.json", UpgradeExpiredRecoveryColdHandbackIntent
                )
                if marker is not None:
                    raise UpgradeUnavailable(
                        "Retained Source helper launch requires explicit failed-launch reconciliation"
                    )
                run.write("cold.handback.launch.json", handback, replace=False)
                verify_handback_boundary(settings, handback, control)
                envs = Path(settings.upgrade_environment_root)
                python = verify_prepared_runtime(
                    envs, envs.parent / "runtimes", effect.source_selection.runtime
                )
                environment = dict(os.environ)
                for key in (
                    "UPGRADE_RECOVERY_ID",
                    "UPGRADE_RECOVERY_SUPERVISION_FD",
                    "UPGRADE_RECOVERY_CORE_API_PORT",
                    "UPGRADE_RECOVERY_SUCCESSION_ID",
                    "UPGRADE_RECOVERY_SUCCESSION_MODE",
                    "PYTHONPATH",
                    "PYTHONHOME",
                    "DYLD_INSERT_LIBRARIES",
                    "LD_PRELOAD",
                ):
                    environment.pop(key, None)
                environment.update(
                    {
                        "NODE_NAME": settings.node_name,
                        "NODE_STATE_DIR": settings.node_state_dir,
                        "UPGRADE_ENVIRONMENT_ROOT": settings.upgrade_environment_root,
                        "UPGRADE_BOOTSTRAP_ROOT": settings.upgrade_bootstrap_root,
                        "UPGRADES_ENABLED": "true",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONNOUSERSITE": "1",
                    }
                )
                child = subprocess.Popen(
                    [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env=environment,
                )
                current = NodeAgentProcessIdentity(
                    pid=child.pid,
                    create_time_s=psutil.Process(child.pid).create_time(),
                    environment_sha256=effect.source_selection.runtime.environment_sha256,
                )
            if observe_source_helper(settings, handback) != current:
                raise UpgradeUnavailable(
                    "Source helper launch has no exact physical acknowledgment"
                )
            helper = UpgradeExpiredRecoveryColdHandbackHelper(
                handback=handback, process=current, observed_at=datetime.now(UTC)
            )
            run.write("cold.handback.helper.json", helper, replace=False)
        if (
            helper.handback != handback
            or observe_source_helper(settings, handback) != helper.process
        ):
            raise UpgradeUnavailable("Source helper receipt is no longer physically live")
        deadline_tick = time.monotonic() + max(
            0, (owner.intent.deadline_at - datetime.now(UTC)).total_seconds()
        )
        while datetime.now(UTC) < owner.intent.deadline_at and time.monotonic() < deadline_tick:
            verify_handback_boundary(settings, handback, control)
            writer_held = source_writer_lock_held(bootstrap)
            status = bootstrap.read("status.json", NativeGuardStatus)
            node = PrivateUpgradeStore(
                Path(settings.node_state_dir) / "upgrades", create=False
            ).read("node-process.json", NativeNodeProcess)
            if (
                status is not None
                and writer_held
                and node is not None
                and status.process == helper.process
                and status.ready
                and 0 <= (datetime.now(UTC) - status.observed_at).total_seconds() <= 5
            ):
                receipt = UpgradeExpiredRecoveryColdHandbackReceipt(
                    helper=helper, status=status, node=node, observed_at=datetime.now(UTC)
                )
                previous = run.read(
                    "cold.handback.receipt.json", UpgradeExpiredRecoveryColdHandbackReceipt
                )
                if previous is not None:
                    if previous.helper != helper:
                        raise UpgradeConflict("Source handback retained receipt has another helper")
                    return previous
                run.write("cold.handback.receipt.json", receipt, replace=False)
                return receipt
            time.sleep(min(0.1, max(0, deadline_tick - time.monotonic())))
        raise UpgradeUnavailable("Source helper readiness exceeded captured handback authority")
