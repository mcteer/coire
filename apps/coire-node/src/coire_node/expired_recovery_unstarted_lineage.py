"""Positive retirement and exact no-effect namespace for an unstarted custodian."""

from pathlib import Path

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoverySupportLaunchIntent,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoverySupportStartup,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.recovery_identity import verify_retired_identity


def observe_unstarted_custodian(
    settings: Settings,
    intent: UpgradeExpiredRecoveryIntent,
) -> UpgradeExpiredRecoverySupportProcess:
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    run = PrivateUpgradeStore(root.path("expired-recovery") / intent.recovery_id, create=False)
    with run.ownership("support.launch.lock"):
        allowed = {
            "startup.json",
            "support.launch.intent.json",
            "support.launch.receipt.json",
            "support.log",
            "support.launch.lock",
        }
        if any(path.name not in allowed for path in run.root.iterdir()):
            raise UpgradeConflict("Unstarted custodian has native effects or unknown private state")
        before = {path.name: path.read_bytes() for path in run.root.glob("*.json")}
        startup = run.read("startup.json", UpgradeExpiredRecoverySupportStartup)
        launch = run.read("support.launch.intent.json", UpgradeExpiredRecoverySupportLaunchIntent)
        receipt = run.read("support.launch.receipt.json", UpgradeExpiredRecoverySupportProcess)
        if (
            startup is None
            or launch is None
            or receipt is None
            or startup.intent != intent
            or launch.startup != startup
            or receipt.intent != intent
            or receipt.runtime != startup.runtime
        ):
            raise UpgradeConflict("Unstarted custodian startup provenance is incomplete")
        verify_retired_identity(launch.launcher)
        verify_retired_identity(receipt.process)
        if before != {path.name: path.read_bytes() for path in run.root.glob("*.json")}:
            raise UpgradeConflict("Unstarted custodian private state changed during observation")
        return receipt
