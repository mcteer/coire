"""Positive local recovery claim with kernel exclusion for entire caller lifetime."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_effect_journal import retained_legacy_retirement_proof
from coire_node.expired_recovery_launchd import fixed_launchctl_probe
from coire_node.expired_recovery_preflight import (
    verify_local_active_owner_identity,
    verify_local_successor_identity,
)
from coire_node.expired_recovery_stop import source_identity
from coire_node.recovery_identity import recovery_kernel_boot_identity, verify_retired_identity
from coire_node.recovery_lease import bind_recovery_lease, require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport


@contextmanager
def resumed_recovery_owner(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
) -> Iterator[UpgradeExpiredRecoveryOwnerClaim]:
    """Resume only the same Core-accepted owner; never adopt a dead/replaced PID.

    Supports lost acknowledgement after Core commit beyond initial transfer TTL.
    No Core acceptance retry, recaptured epoch, readiness or original fence release.
    Another process/boot requires a separately supported audited succession path.
    """
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with bootstrap.ownership("recovery-transfer.lock") as descriptor:
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(receipt.transfer.intent.recovery_id), create=False)
        claim = run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim)
        stop = retained_legacy_retirement_proof(run, receipt)
        if (
            claim is None
            or stop is None
            or claim.consumption != receipt
            or claim.runtime != runtime
            or claim.legacy_stop != stop
            or stop.effect.service is None
        ):
            raise UpgradeConflict("Retained recovery owner scope differs")
        current = control.verify_recovery_owner(claim)
        if current.sha256() != claim.sha256():
            raise UpgradeUnavailable("Current recovery owner differs")
        deadline = receipt.transfer.intent.deadline_at
        verify_local_active_owner_identity(settings, claim)
        if recovery_kernel_boot_identity(deadline) != claim.kernel_boot_identity:
            raise UpgradeConflict("Recovery owner kernel changed")
        verify_retired_identity(receipt.transfer.legacy_guard.process)
        if fixed_launchctl_probe(deadline)(stop.effect.service.target) is not None:
            raise UpgradeUnavailable("Legacy service is loaded during owner resume")
        if source_identity(settings, receipt) != stop.preserved_source:
            raise UpgradeUnavailable("Source identity changed before owner resume")
        previous = run.read("owner.activation.json", UpgradeExpiredRecoveryOwnerClaim)
        if previous is not None and previous.sha256() != claim.sha256():
            raise UpgradeConflict("Retained activation differs")
        if control.verify_recovery_owner(claim).sha256() != claim.sha256():
            raise UpgradeUnavailable("Recovery owner became uncertain during resume")
        verify_local_active_owner_identity(settings, claim)
        if datetime.now(UTC) >= deadline:
            raise UpgradeUnavailable("Recovery resume exceeded captured budget")
        if previous is None:
            run.write("owner.activation.json", claim, replace=False)
        with bind_recovery_lease(bootstrap, claim.sha256(), descriptor):
            yield claim


@contextmanager
def activated_recovery_owner(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
) -> Iterator[UpgradeExpiredRecoveryOwnerClaim]:
    """Retain exact Core acknowledgement under lifetime kernel exclusion.

    No serving/status/bootstrap publication or original maintenance release. A
    retained acknowledgement remains historical and every later effect requires
    fresh live Core authorization plus local identity/exclusion checks.
    """
    with recovery_owner_lease(settings, receipt, runtime, control) as claim:
        accepted = control.accept_recovery_owner(claim)
        if accepted.sha256() != claim.sha256():
            raise UpgradeUnavailable("Core recovery owner acknowledgement differs")
        current = control.verify_recovery_owner(claim)
        if current.sha256() != claim.sha256():
            raise UpgradeUnavailable("Core recovery owner is no longer exact")
        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(receipt.transfer.intent.recovery_id), create=False)
        require_recovery_lease(bootstrap, claim.sha256())
        previous = run.read("owner.activation.json", UpgradeExpiredRecoveryOwnerClaim)
        if previous is not None and previous.sha256() != claim.sha256():
            raise UpgradeConflict("Retained recovery activation differs")
        if previous is None:
            run.write("owner.activation.json", claim, replace=False)
        yield claim


@contextmanager
def recovery_owner_lease(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
) -> Iterator[UpgradeExpiredRecoveryOwnerClaim]:
    """Yield scoped claim only while kernel lease held; not serving/cleanup proof.

    Retained claim is historical after exit/crash, never a live lease. Caller must
    independently gate every later effect and publication under the captured
    recovery budget; this context does not release original admission fences.
    """
    receipt = control.verify_recovery_consumption(receipt)
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with bootstrap.ownership("recovery-transfer.lock") as descriptor:
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(runs.path(receipt.transfer.intent.recovery_id), create=False)
        stop = retained_legacy_retirement_proof(run, receipt)
        if stop is None or stop.effect.service is None:
            raise UpgradeUnavailable("Recovery ownership requires positive managed retirement")
        verify_local_successor_identity(settings, receipt, runtime)
        verify_retired_identity(receipt.transfer.legacy_guard.process)
        if (
            fixed_launchctl_probe(receipt.transfer.expires_at)(stop.effect.service.target)
            is not None
        ):
            raise UpgradeUnavailable("Legacy service became loaded before claim")
        if source_identity(settings, receipt) != stop.preserved_source:
            raise UpgradeUnavailable("Source identity differs before recovery claim")
        boot = recovery_kernel_boot_identity(receipt.transfer.expires_at)
        previous = run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim)
        if previous is not None and (
            previous.consumption != receipt
            or previous.legacy_stop != stop
            or previous.runtime != runtime
            or previous.kernel_boot_identity != boot
        ):
            raise UpgradeConflict("Retained recovery owner claim differs")
        control.verify_recovery_consumption(receipt)
        verify_local_successor_identity(settings, receipt, runtime)
        verify_retired_identity(receipt.transfer.legacy_guard.process)
        if (
            fixed_launchctl_probe(receipt.transfer.expires_at)(stop.effect.service.target)
            is not None
        ):
            raise UpgradeUnavailable("Legacy service became loaded during recovery claim")
        if source_identity(settings, receipt) != stop.preserved_source:
            raise UpgradeUnavailable("Source identity changed during recovery claim")
        if datetime.now(UTC) >= receipt.transfer.expires_at:
            raise UpgradeUnavailable("Recovery claim exceeded captured transfer window")
        if previous is None:
            previous = UpgradeExpiredRecoveryOwnerClaim(
                consumption=receipt,
                legacy_stop=stop,
                process=receipt.consumer,
                runtime=runtime,
                kernel_boot_identity=boot,
                claimed_at=datetime.now(UTC),
            )
            run.write("owner.claim.json", previous, replace=False)
        with bind_recovery_lease(bootstrap, previous.sha256(), descriptor):
            yield previous
