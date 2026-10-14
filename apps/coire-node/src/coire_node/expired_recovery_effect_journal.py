"""Native legacy-stop write-ahead intent; retained uncertainty is not success."""

from datetime import datetime

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryLegacyService,
    UpgradeExpiredRecoveryLegacyStopIntent,
    UpgradeExpiredRecoveryLegacyStopReceipt,
    UpgradeExpiredRecoveryLegacyStopReconciliation,
    UpgradeExpiredRecoveryTransferConsumption,
)
from coire_core.models.upgrades import NativeNodeProcess
from coire_core.upgrade_journal import PrivateUpgradeStore


def retained_legacy_stop_proof(
    run: PrivateUpgradeStore,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
) -> UpgradeExpiredRecoveryLegacyStopReceipt | None:
    """Verify historical preimages only; no fresh ownership/serving qualification."""
    retained = run.read("consumption.json", UpgradeExpiredRecoveryTransferConsumption)
    if retained is None or retained.sha256() != receipt.sha256():
        raise UpgradeConflict("Retained legacy stop consumption differs")
    proof = run.read("legacy-stop.receipt.json", UpgradeExpiredRecoveryLegacyStopReceipt)
    if proof is None:
        return None
    effect = run.read("legacy-stop.intent.json", UpgradeExpiredRecoveryLegacyStopIntent)
    if (
        effect is None
        or effect.sha256() != proof.effect.sha256()
        or effect.recovery_id != receipt.transfer.intent.recovery_id
        or effect.consumption_sha256 != receipt.sha256()
        or effect.legacy_guard != receipt.transfer.legacy_guard
        or effect.successor != receipt.consumer
        or not receipt.consumed_at
        <= effect.started_at
        <= proof.stopped_at
        < receipt.transfer.expires_at
    ):
        raise UpgradeConflict("Retained legacy stop proof or historical epoch differs")
    return proof


def retained_legacy_retirement_proof(
    run: PrivateUpgradeStore,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
) -> (
    UpgradeExpiredRecoveryLegacyStopReceipt | UpgradeExpiredRecoveryLegacyStopReconciliation | None
):
    """Distinct canonical history only; never ownership or fresh physical facts."""
    acknowledged = retained_legacy_stop_proof(run, receipt)
    observed = run.read(
        "legacy-stop.reconciliation.json", UpgradeExpiredRecoveryLegacyStopReconciliation
    )
    if acknowledged is not None and observed is not None:
        raise UpgradeConflict("Legacy retirement has ambiguous retained proof kinds")
    if observed is None:
        return acknowledged
    effect = run.read("legacy-stop.intent.json", UpgradeExpiredRecoveryLegacyStopIntent)
    if observed.consumption != receipt or effect != observed.effect:
        raise UpgradeConflict("Retained observed retirement preimages differ")
    return observed


def begin_legacy_stop(
    run: PrivateUpgradeStore,
    receipt: UpgradeExpiredRecoveryTransferConsumption,
    now: datetime,
    *,
    service: UpgradeExpiredRecoveryLegacyService | None = None,
    definition_sha256: str | None = None,
    preserved_source: NativeNodeProcess | None = None,
) -> tuple[UpgradeExpiredRecoveryLegacyStopIntent, bool]:
    """Return fresh=True only for first durable publication, never on replay.

    Caller must retain kernel exclusion, fresh Core authority and actual identity/
    launchd proofs before effects. Existing intent means reconcile actual state;
    it does not authorize another stop, selection, ownership claim or release.
    """
    receipt = UpgradeExpiredRecoveryTransferConsumption.model_validate_json(
        receipt.model_dump_json()
    )
    with run.ownership("effect.lock"):
        retained = run.read("consumption.json", UpgradeExpiredRecoveryTransferConsumption)
        if retained is None or retained.sha256() != receipt.sha256():
            raise UpgradeConflict("Native effect has no exact retained consumption")
        previous = run.read("legacy-stop.intent.json", UpgradeExpiredRecoveryLegacyStopIntent)
        if previous is not None:
            if (
                previous.recovery_id,
                previous.consumption_sha256,
                previous.legacy_guard,
                previous.successor,
            ) != (
                receipt.transfer.intent.recovery_id,
                receipt.sha256(),
                receipt.transfer.legacy_guard,
                receipt.consumer,
            ):
                raise UpgradeConflict("Native legacy stop retry changes effect authority")
            if not receipt.consumed_at <= previous.started_at < receipt.transfer.expires_at:
                raise UpgradeConflict("Retained legacy stop lies outside captured epoch")
            if (
                previous.service != service
                or previous.definition_sha256 != definition_sha256
                or previous.preserved_source != preserved_source
            ):
                raise UpgradeConflict("Retained legacy stop service definition differs")
            return previous, False
        if now.tzinfo is None or not receipt.consumed_at <= now < receipt.transfer.expires_at:
            raise UpgradeUnavailable("Native legacy stop epoch has elapsed")
        intent = UpgradeExpiredRecoveryLegacyStopIntent(
            recovery_id=receipt.transfer.intent.recovery_id,
            consumption_sha256=receipt.sha256(),
            legacy_guard=receipt.transfer.legacy_guard,
            successor=receipt.consumer,
            started_at=now,
            service=service,
            definition_sha256=definition_sha256,
            preserved_source=preserved_source,
        )
        run.write("legacy-stop.intent.json", intent, replace=False)
        return intent, True
