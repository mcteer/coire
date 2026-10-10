"""Fixed-label contribution telemetry never records prompts, answers or tags."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import metrics, trace
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.audit import write_principal_audit
from coire_api.auth import Principal
from coire_api.db import session_scope
from coire_core.errors import CoireError
from coire_core.models.audit import AuditOutcome

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.api.feedback")
mutations_total = metrics.get_meter("coire.api.feedback").create_counter(
    "coire_feedback_mutations_total", unit="1"
)
purged_total = metrics.get_meter("coire.api.feedback").create_counter(
    "coire_feedback_purged_total", unit="1"
)
purge_oldest_seconds = metrics.get_meter("coire.api.feedback").create_gauge(
    "coire_feedback_purge_oldest_seconds", unit="s"
)


@asynccontextmanager
async def mutation_scope(principal: Principal, operation: str) -> AsyncIterator[AsyncSession]:
    with tracer.start_as_current_span(
        f"coire.api.feedback.{operation}", record_exception=False, set_status_on_exception=False
    ):
        try:
            async with session_scope() as session:
                yield session
        except CoireError as error:
            async with session_scope() as audit_session:
                await write_principal_audit(
                    audit_session,
                    principal=principal,
                    action="feedback.refused",
                    target_type="feedback_operation",
                    target_id=operation,
                    outcome=AuditOutcome.REFUSED,
                    context={"reason": error.code},
                )
                await audit_session.commit()
            mutations_total.add(1, {"operation": operation, "outcome": "refused"})
            logger.info(
                "feedback mutation refused",
                extra={
                    "operation": operation,
                    "safe_reason": error.code,
                    "user_id": str(principal.user_id),
                },
            )
            raise
        logger.info(
            "feedback mutation accepted",
            extra={"operation": operation, "user_id": str(principal.user_id)},
        )
