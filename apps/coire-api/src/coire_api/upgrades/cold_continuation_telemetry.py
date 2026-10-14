"""Committed continuation changes only, with bounded labels and scoped logs."""

import logging

from opentelemetry import metrics

from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryIntent
from coire_core.models.upgrade_recovery_continuation import UpgradeColdContinuationSchemaReceipt

logger = logging.getLogger(__name__)
meter = metrics.get_meter("coire.api.cold_continuation")
accepted = meter.create_counter("coire_upgrade_cold_continuations_total")
prepared = meter.create_counter("coire_upgrade_cold_continuation_schema_preparations_total")


def record_acceptance(intent: UpgradeExpiredRecoveryIntent, created: bool) -> None:
    node = intent.original_plan.operation.node
    if created and node is not None:
        accepted.add(1, {"node": node, "outcome": "accepted"})
        logger.warning(
            "cold continuation accepted with original maintenance retained",
            extra={
                "job_id": intent.request.original_job_id,
                "user_id": str(intent.accepted_by),
                "node": intent.original_plan.operation.node,
                "model_id": str(intent.original_plan.model_id),
                "outcome": "accepted",
            },
        )


def record_schema(receipt: UpgradeColdContinuationSchemaReceipt, created: bool) -> None:
    if created:
        prepared.add(1, {"outcome": "prepared"})
        logger.info(
            "cold continuation schema preparation committed",
            extra={
                "job_id": receipt.original_job_id,
                "user_id": str(receipt.prepared_by),
                "outcome": "prepared",
            },
        )
