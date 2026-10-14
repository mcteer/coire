"""Legacy receipt acknowledgement of canonical resolution; no probe or new budget."""

import fcntl
import hashlib
import logging
import os
import platform
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psutil
from opentelemetry import metrics, trace

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_native_terminal import (
    UpgradeNativeTerminalAcknowledgement,
    UpgradeNativeTerminalGrant,
    UpgradeNativeTerminalIntent,
    UpgradeNativeTerminalServingHistory,
    UpgradeNativeTerminalStagingQuarantine,
    UpgradeNativeTerminalState,
    UpgradeNativeTerminalSupervisorIntent,
    UpgradeNativeTerminalSupervisorReceipt,
)
from coire_core.models.upgrade_recovery import (
    UpgradeColdClosureSupervision,
    UpgradeExpiredRecoveryColdResolution,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
)
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeSmokeAttempt,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
    PreparedNativeRuntime,
    UpgradeEvidence,
    UpgradeMaintenance,
    UpgradeOwnerInventory,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore, UpgradeReceiptJournal
from coire_node.expired_recovery_cold_archive import history_sha256
from coire_node.expired_recovery_cold_supervision import verify_fixture_supervisor
from coire_node.native_platform import is_darwin
from coire_node.upgrade_executor import NativeDiskReservation
from coire_node.upgrade_guard import NativeGuardTransport, _owned_native_process
from coire_node.upgrade_runtime import verify_prepared_runtime
from coire_node.upgrade_unclaimed_cancellation import (
    SupportCancellationControl,
    stable_metadata,
    verify_support_interpreter,
)

tracer = trace.get_tracer("coire.node.native_terminal")
acknowledgements = metrics.get_meter("coire.node.native_terminal").create_counter(
    "coire_upgrade_native_terminal_acknowledgements_total"
)
quarantined_bytes = metrics.get_meter("coire.node.native_terminal").create_gauge(
    "coire_upgrade_native_terminal_quarantined_bytes"
)


def quarantine_rebooted_staging(
    reservation: "NativeDiskReservation", run: PrivateUpgradeStore, resolution_sha256: str
) -> bool:
    """Preserve a reboot-renumbered file; never relax ordinary unlink authority."""
    with reservation.store.ownership("space.lock"):
        hold = reservation.current()
        if hold is None:
            return False
        key = "native-terminal.staging-quarantine.json"
        marker = run.read(key, UpgradeNativeTerminalStagingQuarantine)
        source = reservation.store.path(hold.resource_key)
        destination = run.path("native-terminal.staging-reservation.quarantine")
        if marker is None:
            if hold.state != "held" or not source.exists():
                return False
            details = source.lstat()
            if details.st_dev == hold.file_device:
                return False
            boot = datetime.fromtimestamp(psutil.boot_time(), UTC)
            if (
                boot <= hold.updated_at
                or not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
                or details.st_nlink != 1
                or details.st_ino != hold.file_inode
                or details.st_size != hold.requested_bytes
                or details.st_blocks * 512 < hold.requested_bytes
                or details.st_dev != run.root.stat().st_dev
                or destination.exists()
                or destination.is_symlink()
                or any(
                    p.name not in {"hold.json", "space.lock", hold.resource_key}
                    for p in reservation.store.root.iterdir()
                )
            ):
                raise UpgradeUnavailable("Rebooted staging cannot be safely quarantined")
            marker = UpgradeNativeTerminalStagingQuarantine(
                resolution_sha256=resolution_sha256,
                hold=hold,
                observed_device=details.st_dev,
                observed_inode=details.st_ino,
                observed_size=details.st_size,
                observed_blocks=details.st_blocks,
                observed_mtime_ns=details.st_mtime_ns,
                observed_ctime_ns=details.st_ctime_ns,
                boot_time=boot,
                recorded_at=datetime.now(UTC),
            )
            run.write(key, marker, replace=False)
        if marker.resolution_sha256 != resolution_sha256 or hold not in (
            marker.hold,
            marker.hold.model_copy(update={"state": "released", "updated_at": hold.updated_at}),
        ):
            raise UpgradeConflict("Staging quarantine authority differs")
        if source.exists() or source.is_symlink():
            if destination.exists() or destination.is_symlink():
                raise UpgradeConflict("Staging quarantine is duplicated")
            details = source.lstat()
            if (
                details.st_dev,
                details.st_ino,
                details.st_size,
                details.st_blocks,
                details.st_mtime_ns,
                details.st_ctime_ns,
            ) != (
                marker.observed_device,
                marker.observed_inode,
                marker.observed_size,
                marker.observed_blocks,
                marker.observed_mtime_ns,
                marker.observed_ctime_ns,
            ):
                raise UpgradeConflict("Staging quarantine source changed")
            source.rename(destination)
            reservation.store.sync()
            run.sync()
        details = destination.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_nlink != 1
            or (
                details.st_dev,
                details.st_ino,
                details.st_size,
                details.st_blocks,
                details.st_mtime_ns,
            )
            != (
                marker.observed_device,
                marker.observed_inode,
                marker.observed_size,
                marker.observed_blocks,
                marker.observed_mtime_ns,
            )
        ):
            raise UpgradeConflict("Staging quarantine retained file changed")
        if hold.state != "released":
            reservation._record(
                hold.model_copy(update={"state": "released", "updated_at": datetime.now(UTC)})
            )
        quarantined_bytes.set(
            details.st_blocks * 512, {"node": reservation.plan.operation.node or "unknown"}
        )
        return True


def prefix_hash(journal: UpgradeReceiptJournal, count: int) -> str:
    digest = hashlib.sha256()
    for sequence in range(1, count + 1):
        key = journal.key(sequence)
        path = journal.store.path(key)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before = os.fstat(descriptor)
            if (
                before.st_uid != os.getuid()
                or before.st_mode & 0o077
                or before.st_nlink != 1
                or before.st_size > 512 * 1024
            ):
                raise UpgradeUnavailable("Native original receipt ownership or size differs")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                data = source.read(512 * 1024 + 1)
            if (
                len(data) != before.st_size
                or stable_metadata(os.fstat(descriptor)) != stable_metadata(before)
                or stable_metadata(path.lstat()) != stable_metadata(before)
            ):
                raise UpgradeConflict("Native original receipt changed during observation")
        finally:
            os.close(descriptor)
        digest.update(key.encode())
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def transfer_source_smoke(
    settings: Settings,
    grant: UpgradeNativeTerminalGrant,
    run: PrivateUpgradeStore,
    guard: PrivateUpgradeStore,
) -> None:
    result = grant.resolution
    smoke = result.closure.handback.helper.handback.verification.request.smoke
    plan = smoke.smoke.source.launch.source.projection.owner.intent.original_plan
    attempt = smoke.attempt
    if attempt.state != "complete" or not attempt.cleanup_verified or attempt.evidence is None:
        raise UpgradeConflict("Native acknowledgement has no actual completed recovery smoke")
    runtime = result.closure.node.selected.runtime
    python = verify_prepared_runtime(
        Path(settings.upgrade_environment_root),
        Path(settings.upgrade_environment_root).parent / "runtimes",
        runtime,
    )
    if (
        attempt.process is None
        or _owned_native_process(attempt.process, python, "coire_node.upgrade_smoke") is not None
    ):
        raise UpgradeUnavailable("Native acknowledgement smoke process retirement is unverified")
    existing = guard.read("source-serving.json", NativeSmokeAttempt)
    scope_name = (
        f"{plan.operation.job_id}-{plan.operation.generation}-{runtime.environment_sha256}-source"
    )
    origin = run.root / "smoke-state/upgrades/smokes" / scope_name
    destination_root = PrivateUpgradeStore(
        Path(settings.node_state_dir) / "upgrades/smokes", create=False
    )
    destination = destination_root.path(scope_name)
    if existing is not None and existing != attempt:
        original = PrivateUpgradeStore(destination, create=False)
        recovered = PrivateUpgradeStore(origin, create=False)
        with original.ownership("smoke.lock"), recovered.ownership("smoke.lock"):
            for recorded, scope in ((existing, original), (attempt, recovered)):
                if (
                    recorded.state != "complete"
                    or not recorded.cleanup_verified
                    or recorded.process is None
                    or recorded.evidence is None
                    or (
                        recorded.job_id,
                        recorded.generation,
                        recorded.plan_sha256,
                        recorded.stage,
                        recorded.runtime_sha256,
                    )
                    != (
                        attempt.job_id,
                        attempt.generation,
                        attempt.plan_sha256,
                        "source-serving",
                        runtime.environment_sha256,
                    )
                    or _owned_native_process(recorded.process, python, "coire_node.upgrade_smoke")
                    is not None
                    or scope.read("result.json", UpgradeEvidence) != recorded.evidence
                    or scope.read("owner.json", UpgradeOwnerInventory)
                    != UpgradeOwnerInventory(owner="engines", status="complete")
                ):
                    raise UpgradeConflict("Native serving history lacks exact completed custody")
                if any(
                    getattr(recorded.evidence, field) != getattr(attempt.evidence, field)
                    for field in (
                        "runtime_sha256",
                        "release_sha256",
                        "model_id",
                        "model_variant_id",
                        "model_manifest_sha256",
                    )
                ):
                    raise UpgradeConflict("Native serving history Source binding differs")
            proof = UpgradeNativeTerminalServingHistory(
                resolution_sha256=result.sha256(),
                original_attempt_sha256=existing.sha256(),
                recovered_attempt_sha256=attempt.sha256(),
                original_scope_sha256=history_sha256(original),
                recovered_scope_sha256=history_sha256(recovered),
            )
            key = "native-terminal.serving-history.json"
            retained_history = run.read(key, UpgradeNativeTerminalServingHistory)
            if retained_history is None:
                run.write(key, proof, replace=False)
            elif retained_history != proof:
                raise UpgradeConflict("Native serving history retained bytes changed")
        return
    check_scope = origin if origin.exists() or origin.is_symlink() else destination
    scope_store = PrivateUpgradeStore(check_scope, create=False)
    if scope_store.read("result.json", UpgradeEvidence) != attempt.evidence:
        raise UpgradeConflict(
            "Native acknowledgement serving result differs before custody transfer"
        )
    inventory = scope_store.read("owner.json", UpgradeOwnerInventory)
    if (
        inventory is None
        or inventory.owner != "engines"
        or inventory.status != "complete"
        or inventory.entries
    ):
        raise UpgradeConflict("Native acknowledgement serving ownership is unresolved")
    if origin.exists() or origin.is_symlink():
        if destination.exists() or destination.is_symlink():
            raise UpgradeConflict("Native acknowledgement smoke custody is duplicated")
        # The original completed bytes move once. No new result is generated.
        PrivateUpgradeStore(origin, create=False)
        origin.rename(destination)
        destination_root.sync()
        PrivateUpgradeStore(origin.parent, create=False).sync()
    elif not destination.exists():
        raise UpgradeUnavailable("Native acknowledgement smoke custody is absent")
    retained = PrivateUpgradeStore(destination, create=False).read("result.json", UpgradeEvidence)
    if retained != attempt.evidence:
        raise UpgradeConflict("Native acknowledgement retained serving bytes differ")
    if existing is None:
        guard.write("source-serving.json", attempt, replace=False)


@tracer.start_as_current_span("coire.node.native_terminal", record_exception=False)
def acknowledge_native_terminal(
    settings: Settings, grant: UpgradeNativeTerminalGrant, transport: NativeGuardTransport
) -> UpgradeNativeTerminalAcknowledgement:
    result = grant.resolution
    owner = result.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    plan = owner.intent.original_plan
    job = plan.operation.job_id
    if settings.node_name != plan.operation.node:
        raise UpgradeConflict("Native acknowledgement targets another Studio")
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    run = PrivateUpgradeStore(
        root.root / "expired-recovery" / owner.intent.recovery_id, create=False
    )
    operation = PrivateUpgradeStore(root.root / "operations" / job, create=False)
    guard = PrivateUpgradeStore(operation.root / "guard", create=False)
    control = SupportCancellationControl(settings)
    state_root = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades", create=False)
    with root.ownership("control.lock"), state_root.ownership("maintenance.lock"):
        state = transport._read(
            f"/api/v1/internal/upgrades/{owner.intent.recovery_id}/native-terminal",
            UpgradeNativeTerminalState,
            datetime.now(UTC) + timedelta(seconds=5),
            core=True,
        )
        if (
            state.grant != grant
            or not 0 <= (datetime.now(UTC) - state.observed_at).total_seconds() <= 5
        ):
            raise UpgradeConflict("Native acknowledgement has no fresh exact audited resolution")
        if run.read("cold.resolution.json", UpgradeExpiredRecoveryColdResolution) != result:
            raise UpgradeConflict("Native acknowledgement local canonical resolution differs")
        claim = root.read("claim.json", NativeUpgradeClaim)
        native = guard.read("intent.json", NativeRecoveryIntent)
        maintenance = state_root.read("maintenance.json", UpgradeMaintenance)
        node = state_root.read("node-process.json", NativeNodeProcess)
        selected = control.observed()
        if claim is None or (claim.job_id, claim.generation, claim.state, claim.plan_sha256) != (
            job,
            plan.operation.generation,
            "released",
            plan.authority_sha256(),
        ):
            raise UpgradeConflict("Native acknowledgement original released claim differs")
        if (
            native is None
            or native.phase not in {"source_selected", "source_verified"}
            or native.source.runtime != selected.runtime
            or native.source.release_sha256 != selected.release_sha256
            or node != result.closure.node
        ):
            raise UpgradeConflict(
                "Native acknowledgement Source facts differ from resolved handback"
            )
        if (
            maintenance is None
            or maintenance.state != "released"
            or not control._guard_ready(selected)
        ):
            raise UpgradeUnavailable(
                "Native acknowledgement Source maintenance or guard is unready"
            )
        supervision = run.read(
            "cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt
        )
        if supervision is None or result.closure.supervision not in (
            supervision,
            UpgradeColdClosureSupervision.from_receipt(supervision),
        ):
            raise UpgradeConflict("Native acknowledgement supervision differs")
        if supervision.kind == "fixture":
            try:
                verify_fixture_supervisor(settings, supervision)
            except UpgradeUnavailable:
                adopt_retired_fixture_supervisor(settings, grant, run, supervision)
        else:
            from coire_node.expired_recovery_definition import read_legacy_definition
            from coire_node.expired_recovery_launchd import (
                fixed_launchctl_probe,
                observe_legacy_guard_service,
            )
            from coire_node.expired_recovery_source_supervisor import verify_source_children

            status = root.read("status.json", NativeGuardStatus)
            if status is None:
                raise UpgradeUnavailable("Native acknowledgement managed status is absent")
            service = observe_legacy_guard_service(
                status, os.getuid(), fixed_launchctl_probe(datetime.now(UTC) + timedelta(seconds=5))
            )
            if (
                service.process != supervision.process
                or read_legacy_definition(settings, status, service)
                != supervision.definition_sha256
            ):
                raise UpgradeConflict("Native acknowledgement managed supervision differs")
            verify_source_children(settings, supervision.supervision)
        journal = UpgradeReceiptJournal(operation.root / "receipts", plan)
        marker = run.read("native-terminal.ack.json", UpgradeNativeTerminalAcknowledgement)
        if marker is None:
            latest = journal.latest()
            if latest is None or latest.phase is not UpgradePhase.RECOVERY_REQUIRED:
                raise UpgradeConflict(
                    "Native acknowledgement requires original retained failure history"
                )
            if latest.sequence > 4094:
                raise UpgradeUnavailable("Native acknowledgement exceeds the fixed journal bound")
            evidence = (
                result.closure.handback.helper.handback.verification.request.smoke.attempt.evidence
            )
            if evidence is None:
                raise UpgradeUnavailable(
                    "Native acknowledgement canonical serving evidence is absent"
                )
            proof = evidence.model_copy(update={"source_verified": True, "cleanup_verified": True})
            verification = UpgradeReceipt(
                job_id=job,
                generation=plan.operation.generation,
                sequence=latest.sequence + 1,
                effect_id="after-deadline-source-" + owner.intent.recovery_id.lower(),
                phase=UpgradePhase.VERIFYING_SOURCE,
                occurred_at=datetime.now(UTC),
                outcome="complete",
                observed_sha256=plan.source.manifest_sha256,
                reason="expired_operation_recovered",
                evidence=proof.model_dump_json(),
                restoration="verified",
            )
            terminal = verification.model_copy(
                update={
                    "sequence": verification.sequence + 1,
                    "effect_id": "after-deadline-terminal-" + owner.intent.recovery_id.lower(),
                    "phase": UpgradePhase.ROLLED_BACK,
                }
            )
            marker = UpgradeNativeTerminalAcknowledgement(
                resolution_sha256=result.sha256(),
                original_prefix_sha256=prefix_hash(journal, latest.sequence),
                verification=verification,
                terminal=terminal,
            )
            run.write("native-terminal.ack.json", marker, replace=False)
        if (
            marker.resolution_sha256 != result.sha256()
            or prefix_hash(journal, marker.verification.sequence - 1)
            != marker.original_prefix_sha256
        ):
            raise UpgradeConflict(
                "Native acknowledgement changes canonical resolution or original history"
            )
        native_intent = run.read("native-terminal.intent.json", UpgradeNativeTerminalIntent)
        if native_intent is None:
            projection = result.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection
            if native != projection.projected_native:
                raise UpgradeConflict(
                    "Native acknowledgement has another projected native preimage"
                )
            native_intent = UpgradeNativeTerminalIntent(
                resolution_sha256=result.sha256(),
                before_native=native,
                after_native=native.model_copy(
                    update={"phase": "source_verified", "updated_at": marker.terminal.occurred_at}
                ),
            )
            run.write("native-terminal.intent.json", native_intent, replace=False)
        if native_intent.resolution_sha256 != result.sha256() or native not in (
            native_intent.before_native,
            native_intent.after_native,
        ):
            raise UpgradeConflict("Native acknowledgement mutable intent preimage differs")
        transfer_source_smoke(settings, grant, run, guard)
        # Retire only the original exact sparse staging hold. This is owned
        # terminal-resource cleanup, with no engine or qualification dispatch.
        reservation = Path(settings.upgrade_environment_root) / ".upgrade-reservations" / job
        if reservation.exists() or reservation.is_symlink():
            disk = NativeDiskReservation(reservation, plan)
            if not quarantine_rebooted_staging(disk, run, result.sha256()):
                disk.release()
        else:
            raise UpgradeUnavailable("Native acknowledgement original staging history is absent")
        journal.append(
            marker.verification, resource_identity="recovery-resolution-" + result.sha256()
        )
        journal.append(marker.terminal, resource_identity="recovery-resolution-" + result.sha256())
        if guard.read("intent.json", NativeRecoveryIntent) != native_intent.after_native:
            guard.write("intent.json", native_intent.after_native)
        control.verify_terminal(job, plan.operation.generation)
        acknowledgements.add(1, {"outcome": "verified"})
        return marker


def main() -> None:
    from opentelemetry.sdk.metrics import MeterProvider

    from coire_core.logging import StructuredJSONFormatter
    from coire_node.keychain import NODE_TOKEN_ITEM, read_item
    from coire_node.otel import configure_node_telemetry

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        settings = Settings()
        configure_node_telemetry(
            "native-terminal",
            settings.otlp_endpoint,
            diagnostics_enabled=settings.diagnostics_enabled,
        )
        if not settings.node_token.get_secret_value():
            settings.node_token = read_item(NODE_TOKEN_ITEM)
        if not is_darwin(sys.platform) or platform.machine() != "arm64":
            raise UpgradeUnavailable("Native terminal acknowledgement requires a Studio")
        data = sys.stdin.buffer.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise UpgradeUnavailable("Native acknowledgement input exceeds bound")
        grant = UpgradeNativeTerminalGrant.model_validate_json(data)
        verify_support_interpreter(settings)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            result = acknowledge_native_terminal(
                settings, grant, NativeGuardTransport(settings, client)
            )
        logging.getLogger(__name__).info(
            "Canonical recovery acknowledged by native reader",
            extra={"job_id": result.terminal.job_id, "generation": result.terminal.generation},
        )
        sys.stdout.write(result.model_dump_json())
    except (CoireError, OSError, ValueError, psutil.Error) as error:
        logging.getLogger(__name__).error(
            "Native acknowledgement refused",
            extra={
                "error_type": type(error).__name__,
                "reason": str(error)[:256]
                if isinstance(error, CoireError)
                else "bounded native observation refused",
            },
        )
        acknowledgements.add(1, {"outcome": "refused"})
        raise SystemExit("Native acknowledgement refused; original records retained") from None
    finally:
        provider = metrics.get_meter_provider()
        if isinstance(provider, MeterProvider):
            provider.shutdown(timeout_millis=1000)


def verify_adopted_supervisor(
    settings: Settings, receipt: UpgradeNativeTerminalSupervisorReceipt
) -> None:
    from coire_node.expired_recovery_source_supervisor import verify_source_children

    envs = Path(settings.upgrade_environment_root)
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", receipt.intent.runtime)
    child = _owned_native_process(
        receipt.process, python, "coire_node.upgrade_native_terminal_supervisor"
    )
    lock = Path(settings.upgrade_bootstrap_root).parent / "fixture-records/fixture-supervisor.lock"
    if child is None or not any(Path(f.path) == lock for f in child.open_files()):
        raise UpgradeUnavailable("Native acknowledgement adopted supervisor is unverified")
    owner = receipt.intent.grant.resolution.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    run = PrivateUpgradeStore(
        Path(settings.upgrade_bootstrap_root) / "expired-recovery" / owner.intent.recovery_id,
        create=False,
    )
    old = run.read("cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt)
    if old is None:
        raise UpgradeUnavailable("Native acknowledgement original supervision is absent")
    verify_source_children(settings, old.supervision)


def adopt_retired_fixture_supervisor(
    settings: Settings,
    grant: UpgradeNativeTerminalGrant,
    run: PrivateUpgradeStore,
    old: UpgradeExpiredRecoveryColdSupervisionReceipt,
) -> UpgradeNativeTerminalSupervisorReceipt:
    envs = Path(settings.upgrade_environment_root)
    old_owner = grant.resolution.closure.handback.helper.handback.verification.request.smoke.smoke.source.launch.source.projection.owner
    old_python = verify_prepared_runtime(envs, envs.parent / "runtimes", old_owner.runtime)
    if (
        _owned_native_process(
            old.process, old_python, "coire_node.expired_recovery_source_supervisor"
        )
        is not None
    ):
        raise UpgradeConflict(
            "Native fixture supervision is still owned by its recorded predecessor"
        )
    receipt = run.read(
        "native-terminal.supervisor.receipt.json", UpgradeNativeTerminalSupervisorReceipt
    )
    if receipt is not None:
        if receipt.intent.grant != grant:
            raise UpgradeConflict("Native fixture supervision authority changed")
        verify_adopted_supervisor(settings, receipt)
        return receipt
    marker = run.read(
        "native-terminal.supervisor.intent.json", UpgradeNativeTerminalSupervisorIntent
    )
    if marker is not None:
        raise UpgradeUnavailable(
            "Native fixture supervision launch outcome is unresolved; no repeated launch"
        )
    runtime = PrivateUpgradeStore(envs / ".identities", create=False).read(
        Path(sys.prefix).name + ".json", PreparedNativeRuntime
    )
    if runtime is None:
        raise UpgradeUnavailable("Native fixture supervision support identity is absent")
    python = verify_prepared_runtime(envs, envs.parent / "runtimes", runtime)
    if Path(sys.prefix).resolve(strict=True) != (envs / runtime.environment_id).resolve(
        strict=True
    ):
        raise UpgradeUnavailable("Native fixture supervision launcher interpreter differs")
    lock = Path(settings.upgrade_bootstrap_root).parent / "fixture-records/fixture-supervisor.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size != 0
        ):
            raise UpgradeUnavailable("Native fixture supervision lock ownership differs")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UpgradeConflict("Native fixture supervision exclusion is already held") from None
        marker = UpgradeNativeTerminalSupervisorIntent(
            grant=grant, runtime=runtime, started_at=datetime.now(UTC)
        )
        run.write("native-terminal.supervisor.intent.json", marker, replace=False)
        environment = dict(os.environ)
        environment.update(
            UPGRADE_RECOVERY_ID=old_owner.intent.recovery_id,
            UPGRADE_RECOVERY_SUPERVISION_FD=str(descriptor),
        )
        from coire_node.upgrade_native_terminal_supervisor import private_output

        output = private_output(run.root / "native-terminal.supervisor.log")
        try:
            child = subprocess.Popen(
                [str(python), "-I", "-B", "-m", "coire_node.upgrade_native_terminal_supervisor"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                pass_fds=(descriptor,),
                start_new_session=True,
            )
        finally:
            output.close()
        process = NodeAgentProcessIdentity(
            pid=child.pid,
            create_time_s=psutil.Process(child.pid).create_time(),
            environment_sha256=runtime.environment_sha256,
        )
        run.write(
            "native-terminal.supervisor.launch.json",
            UpgradeNativeTerminalSupervisorReceipt(
                intent=marker, process=process, observed_at=datetime.now(UTC)
            ),
            replace=False,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            receipt = run.read(
                "native-terminal.supervisor.receipt.json", UpgradeNativeTerminalSupervisorReceipt
            )
            if receipt is not None:
                if receipt.intent != marker or receipt.process != process:
                    raise UpgradeConflict("Native fixture supervision receipt differs")
                verify_adopted_supervisor(settings, receipt)
                return receipt
            if child.poll() is not None:
                raise UpgradeUnavailable("Native fixture supervision child refused adoption")
            time.sleep(0.1)
        raise UpgradeUnavailable(
            "Native fixture supervision adoption acknowledgement is unresolved"
        )
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
