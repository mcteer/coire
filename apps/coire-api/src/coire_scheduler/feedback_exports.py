"""ID-only durable export execution; no source bytes enter the DBOS journal."""

import logging

from dbos import DBOS
from opentelemetry import metrics, trace

from coire_api.db import PreferenceExportRow, session_scope
from coire_api.feedback.export_execution import execute_export
from coire_core.settings import get_settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.feedback")
transitions = metrics.get_meter("coire.scheduler.feedback").create_counter(
    "coire_feedback_export_transitions_total", unit="1"
)


@DBOS.step(retries_allowed=True, max_attempts=100, interval_seconds=1)
async def export_tick(export_id: str) -> bool:
    with tracer.start_as_current_span(
        "coire.scheduler.feedback.export",
        attributes={"export_id": export_id},
        record_exception=False,
        set_status_on_exception=False,
    ):
        try:
            state = await execute_export(export_id, get_settings())
            async with session_scope() as session:
                export = await session.get(PreferenceExportRow, export_id)
                complete = export is None or (
                    export.state in {"succeeded", "failed", "cancelled"}
                    and not export.cleanup_pending
                )
            transitions.add(1, {"state": state if state != "missing" else "failed"})
            return complete
        except Exception as error:
            logger.error(
                "feedback export recovery deferred",
                extra={"export_id": export_id, "safe_reason": type(error).__name__},
            )
            # DBOS persists exceptions; discard database exception parameters.
            raise RuntimeError("Feedback export recovery unavailable") from None


@DBOS.workflow(name="coire.feedback.export", max_recovery_attempts=100)
async def export_workflow(export_id: str) -> None:
    while not await export_tick(export_id):
        await DBOS.sleep_async(1)
