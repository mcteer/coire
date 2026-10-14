"""Explicit cancellation authority for a never-published native preparation."""

from uuid import UUID

from pydantic import AwareDatetime, model_validator

from coire_core.models.upgrades import (
    Sha256,
    UpgradeExecutionPlan,
    UpgradePhase,
    UpgradeWire,
)


class UpgradeUnclaimedCancellationRequest(UpgradeWire):
    plan_sha256: Sha256


class UpgradeUnclaimedCancellationGrant(UpgradeWire):
    plan: UpgradeExecutionPlan
    requested_by: UUID
    requested_at: AwareDatetime

    @model_validator(mode="after")
    def unstarted(self) -> "UpgradeUnclaimedCancellationGrant":
        operation = self.plan.operation
        if (
            operation.node is None
            or operation.phase is not UpgradePhase.ACCEPTED
            or operation.receipt_sequence != 0
            or operation.finished_at is not None
        ):
            raise ValueError("Cancellation authority requires an unstarted Studio operation")
        return self


class UpgradeUnclaimedCancellationState(UpgradeWire):
    grant: UpgradeUnclaimedCancellationGrant
    observed_at: AwareDatetime


class UpgradeUnclaimedPreparationArchive(UpgradeWire):
    grant: UpgradeUnclaimedCancellationGrant
    files: dict[str, Sha256]
    prepared_at: AwareDatetime


class UpgradeUnclaimedCancellationAudit(UpgradeWire):
    plan_sha256: Sha256
    requested_by: UUID
    requested_at: AwareDatetime
