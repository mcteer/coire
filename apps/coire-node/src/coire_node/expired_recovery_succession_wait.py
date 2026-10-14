"""Passive authenticated grant polling; never infer authority from local absence."""

import threading
import time
from datetime import UTC, datetime

from opentelemetry import trace

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
)
from coire_core.settings import Settings
from coire_node.expired_recovery_support import verify_passive_support_identity
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.recovery_succession")


@tracer.start_as_current_span("coire.node.recovery_succession_wait")
def wait_for_succession_grant(
    settings: Settings,
    previous: UpgradeExpiredRecoveryOwnerClaim,
    succession_id: str,
    control: NativeGuardTransport,
    cancel: threading.Event,
) -> UpgradeExpiredRecoverySuccessionGrant | None:
    """No namespace/claim writes, native stops, serving publication or fallback.

    Delivery metadata is not a lifetime lease. Initial acquisition must subsequently
    hold actual exclusion and repeat all physical/Core gates inside the original
    short grant epoch. Errors propagate; only explicit authenticated pending polls.
    """
    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(previous.model_dump_json())
    intent = previous.consumption.transfer.intent
    # A wall-clock rollback must not replenish this passive session's budget.
    # Core's immutable deadline remains authoritative; this is an extra local cap.
    deadline_tick = time.monotonic() + max(
        0.0, min(300.0, (intent.deadline_at - datetime.now(UTC)).total_seconds())
    )

    def remaining_budget() -> float:
        return min(
            (intent.deadline_at - datetime.now(UTC)).total_seconds(),
            deadline_tick - time.monotonic(),
        )

    successor = None
    while not cancel.is_set():
        now = datetime.now(UTC)
        if now >= intent.deadline_at or remaining_budget() <= 0:
            raise UpgradeUnavailable("Passive succession original budget expired")
        current = verify_passive_support_identity(settings, intent, previous.runtime)
        if cancel.is_set():
            return None
        if remaining_budget() <= 0:
            raise UpgradeUnavailable("Passive succession identity exceeded original budget")
        if successor is not None and current != successor:
            raise UpgradeUnavailable("Passive succession identity changed")
        successor = current
        state = control.read_recovery_succession(previous, succession_id, successor)
        if cancel.is_set():
            return None
        now = datetime.now(UTC)
        if now >= intent.deadline_at or remaining_budget() <= 0:
            raise UpgradeUnavailable("Passive succession delivery exceeded original budget")
        if state.grant is not None:
            if now >= state.grant.expires_at:
                raise UpgradeUnavailable("Passive succession initial grant expired")
            if verify_passive_support_identity(settings, intent, previous.runtime) != successor:
                raise UpgradeUnavailable("Passive succession identity changed during delivery")
            if cancel.is_set():
                return None
            now = datetime.now(UTC)
            if now >= min(intent.deadline_at, state.grant.expires_at) or remaining_budget() <= 0:
                raise UpgradeUnavailable("Passive succession grant expired during verification")
            return state.grant
        # Only explicit live pending permits passive resampling, never effects.
        from coire_node.expired_recovery_succession import observe_same_boot_succession

        proof = observe_same_boot_succession(settings, succession_id, previous, control)
        if proof.successor != successor:
            raise UpgradeUnavailable("Passive succession observation identity differs")
        if cancel.is_set():
            return None
        if remaining_budget() <= 0:
            raise UpgradeUnavailable("Passive succession observation exceeded original budget")
        control.publish_recovery_succession_observation(proof)
        if cancel.is_set():
            return None
        remaining = remaining_budget()
        if remaining <= 0:
            raise UpgradeUnavailable("Passive succession observation exceeded original budget")
        cancel.wait(min(1.0, remaining))
    return None
