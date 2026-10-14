"""Legacy-native acknowledgement of an already committed after-deadline resolution."""

from uuid import UUID

from pydantic import AwareDatetime, model_validator

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdResolution
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRecoveryIntent,
    NativeUpgradeClaim,
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


class UpgradeNativeFixtureRetirement(UpgradeWire):
    """Positive retirement of an adopted acceptance owner and its current children.

    This is fixture provenance, never recovery authority or a replacement for
    the original native claim, plan, selector, receipts or captured deadlines.
    """

    claim: NativeUpgradeClaim
    native: NativeRecoveryIntent
    guard: NativeGuardStatus
    node_process: NativeNodeProcess
    adoption: UpgradeNativeTerminalSupervisorReceipt
    definition_sha256: Sha256
    observed_at: AwareDatetime
    retired_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def exact_current_source(self) -> "UpgradeNativeFixtureRetirement":
        source = self.native.source
        if (
            self.claim.state != "active"
            or self.native.phase != "recovery_required"
            or (self.claim.job_id, self.claim.generation, self.claim.plan_sha256)
            != (self.native.job_id, self.native.generation, self.native.plan_sha256)
            or self.claim.node != source.node
            or self.guard.node != source.node
            or self.guard.selected is None
            or self.guard.selected.runtime != source.runtime
            or self.guard.selected.release_sha256 != source.release_sha256
            or self.node_process.selected.node != source.node
            or self.node_process.selected.runtime != source.runtime
            or self.node_process.selected.release_sha256 != source.release_sha256
            or (self.node_process.selected.job_id, self.node_process.selected.generation)
            != (self.claim.job_id, self.claim.generation)
            or self.adoption.process.environment_sha256
            != self.adoption.intent.runtime.environment_sha256
            or self.adoption.observed_at > self.observed_at
            or self.native.updated_at > self.observed_at
            or (self.retired_at is not None and self.retired_at < self.observed_at)
        ):
            raise ValueError("fixture retirement is not the exact fenced current Source")
        return self


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
