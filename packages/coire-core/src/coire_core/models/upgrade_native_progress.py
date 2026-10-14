"""Failure-only reconciliation from an independent native guard."""

from itertools import pairwise

from pydantic import model_validator

from coire_core.models.upgrades import (
    NativeRecoveryIntent,
    UpgradePhase,
    UpgradeReceiptBatch,
    UpgradeWire,
)


class NativeRecoveryFailureReplay(UpgradeWire):
    native: NativeRecoveryIntent
    batch: UpgradeReceiptBatch

    @model_validator(mode="after")
    def exact_expired_failure(self) -> "NativeRecoveryFailureReplay":
        native = self.native
        receipts = self.batch.receipts
        last = receipts[-1]
        if (
            native.phase != "recovery_required"
            or native.recovery_deadline_at is None
            or last.phase is not UpgradePhase.RECOVERY_REQUIRED
            or last.outcome != "unknown"
            or last.reason != "native_recovery_deadline"
            or last.occurred_at < native.recovery_deadline_at
            or any((r.job_id, r.generation) != (native.job_id, native.generation) for r in receipts)
            or any(b.sequence != a.sequence + 1 for a, b in pairwise(receipts))
        ):
            raise ValueError("native failure replay lacks exact retained expired history")
        return self
