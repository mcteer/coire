"""Keep one accepted operation moving while its independent kernel owner is held."""

import asyncio
import logging
from datetime import UTC, datetime

from opentelemetry import metrics

from coire_core.errors import CoireError
from coire_core.models.upgrades import TERMINAL_UPGRADE_PHASES, UpgradePhase
from coire_core.settings import Settings

from .executor import CoreExecutor
from .workflow import advance_core_step

logger = logging.getLogger(__name__)
meter = metrics.get_meter("coire.upgrade_executor.worker")
failures = meter.create_counter("coire_upgrade_executor_step_failures_total", unit="")
phase_age = meter.create_gauge("coire_upgrade_executor_phase_age_seconds", unit="s")
deadline_overdue = meter.create_gauge("coire_upgrade_executor_deadline_overdue_seconds", unit="s")


def observe_deadlines(executor: CoreExecutor) -> None:
    """Report original budgets; metrics cannot grant renewed mutation authority."""
    state = executor.journal.state()
    latest = executor.journal.receipts.latest()
    now = datetime.now(UTC)
    terminal = latest is not None and latest.phase in TERMINAL_UPGRADE_PHASES
    verified = latest is not None and (
        latest.phase is UpgradePhase.VERIFYING_SOURCE and latest.outcome == "complete"
        if state.recovery_captured_at is not None
        else latest.phase is UpgradePhase.COMMITTING
        or (latest.phase is UpgradePhase.VERIFYING and latest.outcome == "complete")
    )
    for boundary, deadline in (
        ("runtime", state.readiness_deadline),
        ("outage", state.outage_deadline),
        ("recovery", state.recovery_deadline),
    ):
        relevant = not terminal and not verified and deadline is not None
        if boundary != "recovery" and state.recovery_captured_at is not None:
            relevant = False
        deadline_overdue.set(
            max(0, (now - deadline).total_seconds()) if relevant and deadline is not None else 0,
            {"boundary": boundary},
        )
    # Individual service and migration windows may expire before the global
    # cohort budget. Use the same immutable boundaries as recovery, without
    # capturing a new epoch merely to report their age.
    forward = (
        executor.journal.expired_forward_deadlines(now=now)
        if not terminal and not verified and state.recovery_captured_at is None
        else ()
    )
    deadline_overdue.set(
        max(0, (now - min(forward)).total_seconds()) if forward else 0,
        {"boundary": "forward"},
    )


async def run_owned_operation(
    executor: CoreExecutor, settings: Settings, stop: asyncio.Event
) -> None:
    """Retry retained steps, never claim another plan or release uncertain ownership.

    The supervisor supplies a physically reconciled, held authority. Shutdown leaves
    its durable claim intact for that supervisor's restart path; this loop neither
    advertises bootstrap readiness nor transfers ownership based on process death.
    """
    operation = executor.journal.plan.operation
    while not stop.is_set():
        # A changed kernel/namespace owner is fatal to this loop. Ordinary step
        # failures are retried under the same immutable plan and original budgets.
        executor.authority.verify()
        try:
            await advance_core_step(executor)
        except CoireError as error:
            failures.add(1, {"reason": error.code})
            logger.warning(
                "accepted Core upgrade step remains unresolved",
                extra={"job_id": operation.job_id, "reason": error.code},
            )
        latest = executor.journal.receipts.latest()
        observe_deadlines(executor)
        if latest is not None:
            phase_age.set(
                max(0, (datetime.now(UTC) - latest.occurred_at).total_seconds()),
                {"phase": latest.phase.value},
            )
            if (
                latest.phase in TERMINAL_UPGRADE_PHASES
                and executor.journal.receipts.acknowledged_sequence() == latest.sequence
                and executor.authority.claim.state == "committed"
            ):
                return
        try:
            async with asyncio.timeout(settings.upgrade_poll_s):
                await stop.wait()
        except TimeoutError:
            pass
