"""Legacy-native acknowledgement of an already committed after-deadline resolution."""

from uuid import UUID

from pydantic import AwareDatetime

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdResolution
from coire_core.models.upgrades import (
    NativeRecoveryIntent,
    NodeAgentProcessIdentity,
    PreparedNativeRuntime,
    Sha256,
    UpgradeDiskHold,
    UpgradeReceipt,
    UpgradeWire,
)


class UpgradeNativeTerminalRequest(UpgradeWire):
    resolution_sha256: Sha256


class UpgradeNativeTerminalGrant(UpgradeWire):
    resolution: UpgradeExpiredRecoveryColdResolution
    approved_by: UUID
    approved_at: AwareDatetime


class UpgradeNativeTerminalState(UpgradeWire):
    grant: UpgradeNativeTerminalGrant
    observed_at: AwareDatetime


class UpgradeNativeTerminalAcknowledgement(UpgradeWire):
    resolution_sha256: Sha256
    original_prefix_sha256: Sha256
    verification: UpgradeReceipt
    terminal: UpgradeReceipt


class UpgradeNativeTerminalIntent(UpgradeWire):
    resolution_sha256: Sha256
    before_native: "NativeRecoveryIntent"
    after_native: "NativeRecoveryIntent"


class UpgradeNativeTerminalSupervisorIntent(UpgradeWire):
    grant: UpgradeNativeTerminalGrant
    runtime: "PreparedNativeRuntime"
    started_at: AwareDatetime


class UpgradeNativeTerminalSupervisorReceipt(UpgradeWire):
    intent: UpgradeNativeTerminalSupervisorIntent
    process: "NodeAgentProcessIdentity"
    observed_at: AwareDatetime


class UpgradeNativeTerminalStagingQuarantine(UpgradeWire):
    resolution_sha256: Sha256
    hold: "UpgradeDiskHold"
    observed_device: int
    observed_inode: int
    observed_size: int
    observed_blocks: int
    observed_mtime_ns: int
    observed_ctime_ns: int
    boot_time: AwareDatetime
    recorded_at: AwareDatetime
