"""Retain pending local transfer scope; never infer or change runtime ownership."""

from pathlib import Path

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryTransferConsumption
from coire_core.models.upgrades import PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_preflight import verify_local_transfer_preflight
from coire_node.upgrade_guard import NativeGuardTransport


def prepare_local_transfer(
    settings: Settings,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    runtime: PreparedNativeRuntime,
    control: NativeGuardTransport,
) -> tuple[UpgradeExpiredRecoveryTransferConsumption, bool]:
    """Producer-owned metadata, not a grant, ownership claim or completed handover.

    Verify Core before even creating the new scoped namespace; original journals
    remain untouched. Full identity preflight then another live Core check precede
    immutable publication. Later native effects require separate launchd/exclusion
    checks, fresh Core gates and a crash-safe effect journal—not this marker alone.
    """
    receipt = control.verify_recovery_consumption(receipt)
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with bootstrap.ownership("recovery-transfer.lock"):
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"))
        run = PrivateUpgradeStore(runs.path(receipt.transfer.intent.recovery_id))
        previous = run.read("consumption.json", UpgradeExpiredRecoveryTransferConsumption)
        if previous is not None:
            if previous.sha256() != receipt.sha256():
                raise UpgradeConflict("Local recovery preparation changes consumed scope")
            return previous, False
        verify_local_transfer_preflight(settings, receipt, runtime)
        receipt = control.verify_recovery_consumption(receipt)
        run.write("consumption.json", receipt, replace=False)
        return receipt, True
