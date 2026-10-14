"""Read exact partial handback history without changing released authority."""

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClosureIntent,
    UpgradeExpiredRecoveryColdHandbackIntent,
    UpgradeExpiredRecoveryColdHandbackReceipt,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryColdSupervisionReceipt,
    UpgradeExpiredRecoveryColdVerification,
)
from coire_core.upgrade_journal import PrivateUpgradeStore


def retained_post_handback(
    run: PrivateUpgradeStore,
    source: UpgradeExpiredRecoveryColdSourceProcess,
    core: UpgradeExpiredRecoveryColdHandbackReceipt | None,
) -> (
    tuple[
        UpgradeExpiredRecoveryColdHandbackReceipt,
        UpgradeExpiredRecoveryColdSupervisionReceipt,
        UpgradeExpiredRecoveryColdClosureIntent,
    ]
    | None
):
    keys = (
        "cold.verification.json",
        "cold.handback.intent.json",
        "cold.handback.receipt.json",
        "cold.supervision.receipt.json",
        "cold.closure.intent.json",
    )
    if core is None:
        if any(run.path(key).exists() or run.path(key).is_symlink() for key in keys):
            raise UpgradeUnavailable("Cold handback outcome is partial or uncommitted")
        return None
    handback = run.read("cold.handback.receipt.json", UpgradeExpiredRecoveryColdHandbackReceipt)
    supervision = run.read(
        "cold.supervision.receipt.json", UpgradeExpiredRecoveryColdSupervisionReceipt
    )
    closure = run.read("cold.closure.intent.json", UpgradeExpiredRecoveryColdClosureIntent)
    verification = run.read("cold.verification.json", UpgradeExpiredRecoveryColdVerification)
    handback_intent = run.read(
        "cold.handback.intent.json", UpgradeExpiredRecoveryColdHandbackIntent
    )
    if (
        handback != core
        or supervision is None
        or closure is None
        or handback.helper.handback.verification != verification
        or handback.helper.handback != handback_intent
        or handback.helper.handback.verification.request.smoke.smoke.source != source
        or supervision.supervision.handback != handback
        or closure.supervision != supervision
        or run.path("cold.closure.receipt.json").exists()
        or run.path("cold.resolution.json").exists()
    ):
        raise UpgradeConflict("Cold partial closure history has another or uncertain provenance")
    return handback, supervision, closure
