"""Bounded cold-recovery outcomes; identifiers belong only in structured logs."""

import logging
from typing import Literal

from opentelemetry import metrics

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdClaim

logger = logging.getLogger(__name__)
effects = metrics.get_meter("coire.node.cold_recovery").create_counter(
    "coire_upgrade_cold_recovery_effects_total"
)


def record_cold_effect(
    claim: UpgradeExpiredRecoveryColdClaim,
    outcome: Literal["source_selected", "source_launched", "source_guard_ready", "refused"],
) -> None:
    effects.add(1, {"node": claim.original_claim.node, "outcome": outcome})
    logger.info(
        "cold recovery effect retained",
        extra={
            "job_id": claim.intent.request.original_job_id,
            "recovery_id": claim.intent.recovery_id,
            "generation": claim.original_claim.generation,
            "user_id": str(claim.intent.accepted_by),
            "node": claim.original_claim.node,
            "outcome": outcome,
        },
    )
