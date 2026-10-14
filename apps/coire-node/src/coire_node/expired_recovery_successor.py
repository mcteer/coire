"""Accepted exact-successor lifetime exclusion, never an initial takeover path."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessorClaim,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_launchd import fixed_launchctl_probe
from coire_node.expired_recovery_stop import source_identity
from coire_node.expired_recovery_support import verify_passive_support_identity
from coire_node.recovery_identity import recovery_kernel_boot_identity, verify_retired_identity
from coire_node.recovery_lease import bind_recovery_lease, require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.recovery_successor")


def verify_current_successor(
    settings: Settings,
    claim: UpgradeExpiredRecoverySuccessorClaim,
    control: NativeGuardTransport,
    *,
    initial: bool = False,
) -> None:
    """Repeat Core, current verified process, retirement, kernel and Source."""
    original = claim.grant.observation.previous_owner
    intent = original.consumption.transfer.intent
    service = original.legacy_stop.effect.service
    if service is None:
        raise UpgradeUnavailable("Successor requires known managed retirement")
    verify_successor_core(claim, control, initial=initial)
    if verify_passive_support_identity(settings, intent, claim.runtime) != claim.process:
        raise UpgradeUnavailable("Successor current support identity differs")
    verify_retired_identity(original.process)
    verify_retired_identity(original.consumption.transfer.legacy_guard.process)
    if recovery_kernel_boot_identity(intent.deadline_at) != claim.kernel_boot_identity:
        raise UpgradeUnavailable("Successor kernel identity changed")
    if fixed_launchctl_probe(intent.deadline_at)(service.target) is not None:
        raise UpgradeUnavailable("Legacy service loaded during successor resume")
    if source_identity(settings, original.consumption) != claim.preserved_source:
        raise UpgradeUnavailable("Successor Source preservation changed")
    verify_successor_core(claim, control, initial=initial)
    if not claim.claimed_at <= datetime.now(UTC) < intent.deadline_at:
        raise UpgradeUnavailable("Successor authority changed at boundary")
    # Final Core I/O may outlive the first physical sample. Recheck the current
    # process and preserved serving identity before any caller effect can start.
    if verify_passive_support_identity(settings, intent, claim.runtime) != claim.process:
        raise UpgradeUnavailable("Successor support identity changed during Core check")
    if (
        recovery_kernel_boot_identity(intent.deadline_at) != claim.kernel_boot_identity
        or source_identity(settings, original.consumption) != claim.preserved_source
    ):
        raise UpgradeUnavailable("Successor kernel or Source changed during Core check")
    verify_retired_identity(original.process)
    verify_retired_identity(original.consumption.transfer.legacy_guard.process)
    if fixed_launchctl_probe(intent.deadline_at)(service.target) is not None or datetime.now(
        UTC
    ) >= min(intent.deadline_at, claim.grant.expires_at if initial else intent.deadline_at):
        raise UpgradeUnavailable("Successor final physical gate is no longer live")


def verify_successor_core(
    claim: UpgradeExpiredRecoverySuccessorClaim, control: NativeGuardTransport, *, initial: bool
) -> None:
    if initial:
        state = control.probe_recovery_successor(claim)
        if (
            state.owner is not None
            or state.claim_sha256 != claim.sha256()
            or datetime.now(UTC) >= claim.grant.expires_at
        ):
            raise UpgradeUnavailable("Initial successor lacks exact pending authority")
    elif control.verify_recovery_successor(claim).sha256() != claim.sha256():
        raise UpgradeUnavailable("Successor Core owner differs")


@contextmanager
def initial_successor_owner(
    settings: Settings, claim: UpgradeExpiredRecoverySuccessorClaim, control: NativeGuardTransport
) -> Iterator[UpgradeExpiredRecoverySuccessorClaim]:
    """One initial attempt under real continuous exclusion; no blind retries.

    Uncertain acceptance propagates. Retained exact preimage permits explicit Core
    state inspection, never renewed epochs. No Source publication/fence release.
    """
    claim = UpgradeExpiredRecoverySuccessorClaim.model_validate_json(claim.model_dump_json())
    original = claim.grant.observation.previous_owner
    deadline = min(original.consumption.transfer.intent.deadline_at, claim.grant.expires_at)
    deadline_tick = time.monotonic() + max(
        0.0, min(30.0, (deadline - datetime.now(UTC)).total_seconds())
    )

    def require_initial_budget() -> None:
        from coire_node.expired_recovery_successor_controller import refuse_expired_budget

        with tracer.start_as_current_span("coire.node.recovery_successor_initial_budget"):
            if datetime.now(UTC) >= deadline or time.monotonic() >= deadline_tick:
                refuse_expired_budget(settings, claim, "Initial successor captured budget expired")

    require_initial_budget()
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with bootstrap.ownership("recovery-transfer.lock") as descriptor:
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(
            runs.path(original.consumption.transfer.intent.recovery_id), create=False
        )
        if run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim) != original:
            raise UpgradeConflict("Initial successor retained predecessor differs")
        name = f"successor.{claim.grant.observation.succession_id}.claim.json"
        if run.read(name, UpgradeExpiredRecoverySuccessorClaim) is not None:
            raise UpgradeConflict("Successor preimage retained; inspect explicit Core state")
        verify_current_successor(settings, claim, control, initial=True)
        with bind_recovery_lease(bootstrap, claim.sha256(), descriptor):
            require_recovery_lease(bootstrap, claim.sha256())
            require_initial_budget()
            run.write(name, claim, replace=False)
            verify_current_successor(settings, claim, control, initial=True)
            require_recovery_lease(bootstrap, claim.sha256())
            require_initial_budget()
            if control.accept_recovery_successor(claim).sha256() != claim.sha256():
                raise UpgradeUnavailable("Successor acceptance differs")
            require_live_successor(settings, claim, control)
            yield claim


def require_live_successor(
    settings: Settings, claim: UpgradeExpiredRecoverySuccessorClaim, control: NativeGuardTransport
) -> None:
    """Mandatory effect boundary gate: DTO cannot substitute for descriptor."""
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    require_recovery_lease(bootstrap, claim.sha256())
    verify_current_successor(settings, claim, control)
    require_recovery_lease(bootstrap, claim.sha256())


@contextmanager
def resumed_successor_owner(
    settings: Settings, claim: UpgradeExpiredRecoverySuccessorClaim, control: NativeGuardTransport
) -> Iterator[UpgradeExpiredRecoverySuccessorClaim]:
    """Resume only an already Core-accepted exact current successor.

    No initial acceptance, Source/status/runtime publication, readiness, signals,
    original owner rewrite or fence release. Same-kernel scope only. Real private
    exclusion remains continuously held through caller lifetime and exceptions.
    """
    claim = UpgradeExpiredRecoverySuccessorClaim.model_validate_json(claim.model_dump_json())
    original = claim.grant.observation.previous_owner
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    with (
        tracer.start_as_current_span("coire.node.recovery_successor_resume"),
        bootstrap.ownership("recovery-transfer.lock") as descriptor,
    ):
        runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
        run = PrivateUpgradeStore(
            runs.path(original.consumption.transfer.intent.recovery_id), create=False
        )
        if run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim) != original:
            raise UpgradeConflict("Successor retained initial owner differs")
        verify_current_successor(settings, claim, control)
        with bind_recovery_lease(bootstrap, claim.sha256(), descriptor):
            require_live_successor(settings, claim, control)
            yield claim
