"""Positive retirement and exact no-effect namespace for an unstarted custodian."""

from pathlib import Path

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoverySupportLaunchIntent,
    UpgradeExpiredRecoverySupportProcess,
    UpgradeExpiredRecoverySupportStartup,
)
from coire_core.models.upgrade_recovery_continuation import UpgradeColdSourceKeeperRetirement
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


def observe_retired_keeper_custodian(
    settings: Settings, intent: UpgradeExpiredRecoveryIntent
) -> tuple[UpgradeExpiredRecoverySupportProcess, UpgradeColdSourceKeeperRetirement]:
    """Retain declared preclaim retirement effects; never call them absent."""
    from coire_core.models.upgrades import NodeMaintenanceCommand, UpgradeMaintenance

    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    run = PrivateUpgradeStore(root.path("expired-recovery") / intent.recovery_id, create=False)
    with run.ownership("support.launch.lock"):
        allowed = {
            "startup.json",
            "support.launch.intent.json",
            "support.launch.receipt.json",
            "support.log",
            "support.launch.lock",
            "source.keeper.retirement.intent.json",
            "source.keeper.retirement.receipt.json",
            "local.previous.released.maintenance.json",
            "local.recovery.fence.intent.json",
        }
        if any(path.name not in allowed for path in run.root.iterdir()):
            raise UpgradeConflict("Keeper preclaim custody has unknown or downstream effects")
        if intent.local_recovery_maintenance is None:
            raise UpgradeConflict("Keeper preclaim custody has no captured local fence")
        before = {p.name: p.read_bytes() for p in run.root.glob("*.json")}
        startup = run.read("startup.json", UpgradeExpiredRecoverySupportStartup)
        launch = run.read("support.launch.intent.json", UpgradeExpiredRecoverySupportLaunchIntent)
        receipt = run.read("support.launch.receipt.json", UpgradeExpiredRecoverySupportProcess)
        marker = run.read("source.keeper.retirement.intent.json", UpgradeColdSourceKeeperRetirement)
        retired = run.read(
            "source.keeper.retirement.receipt.json", UpgradeColdSourceKeeperRetirement
        )
        previous = run.read("local.previous.released.maintenance.json", UpgradeMaintenance)
        fence = run.read("local.recovery.fence.intent.json", NodeMaintenanceCommand)
        if (
            startup is None
            or launch is None
            or receipt is None
            or marker is None
            or retired is None
            or retired.retired_at is None
            or retired.grant.intent != intent
            or marker.grant != retired.grant
            or marker.actor != retired.actor
            or marker.started_at != retired.started_at
            or marker.retired_at is not None
            or receipt.process != retired.actor
            or startup.intent != intent
            or launch.startup != startup
            or receipt.intent != intent
            or receipt.runtime != startup.runtime
            or retired.grant.binding.post_handback is None
            or previous != retired.grant.binding.post_handback.released_maintenance
            or fence
            != NodeMaintenanceCommand(maintenance=intent.local_recovery_maintenance, action="apply")
        ):
            raise UpgradeConflict(
                "Keeper preclaim retirement or unapplied fence provenance differs"
            )
        verify_retired_identity(launch.launcher)
        verify_retired_identity(receipt.process)
        verify_retired_identity(retired.grant.binding.post_handback.source_helper.process)
        if before != {p.name: p.read_bytes() for p in run.root.glob("*.json")}:
            raise UpgradeConflict("Keeper preclaim custody changed while observed")
        return receipt, retired
