"""Passive same-kernel crash observation; never absence-based takeover."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry import trace

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionObservation,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_launchd import fixed_launchctl_probe
from coire_node.expired_recovery_stop import source_identity
from coire_node.expired_recovery_support import verify_passive_support_identity
from coire_node.recovery_identity import recovery_kernel_boot_identity, verify_retired_identity
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.recovery_succession")


def observe_same_boot_succession(
    settings: Settings,
    succession_id: str,
    previous: UpgradeExpiredRecoveryOwnerClaim,
    control: NativeGuardTransport,
) -> UpgradeExpiredRecoverySuccessionObservation:
    """Read exact live predecessor scope, not borrow its effect permission.

    Actual current support identity and private exclusion/retirement facts are
    distinct from Core metadata authority. No new claim, startup, budget, state
    namespace or Source mutation occurs. Later audited succession is mandatory.
    """
    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(previous.model_dump_json())
    intent = previous.consumption.transfer.intent
    with tracer.start_as_current_span("coire.node.recovery_succession_observe"):
        if control.verify_recovery_owner(previous).sha256() != previous.sha256():
            raise UpgradeUnavailable("Succession predecessor Core state differs")
        bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        with bootstrap.ownership("recovery-transfer.lock"):
            runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
            run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
            if run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim) != previous:
                raise UpgradeConflict("Succession retained predecessor differs")
            service = previous.legacy_stop.effect.service
            if service is None:
                raise UpgradeUnavailable("Succession requires known managed legacy retirement")
            successor = verify_passive_support_identity(settings, intent, previous.runtime)
            for _ in range(2):
                if control.verify_recovery_owner(previous).sha256() != previous.sha256():
                    raise UpgradeUnavailable("Succession predecessor authority changed")
                if verify_passive_support_identity(settings, intent, previous.runtime) != successor:
                    raise UpgradeUnavailable("Succession current support process changed")
                verify_retired_identity(previous.process)
                verify_retired_identity(previous.consumption.transfer.legacy_guard.process)
                if (
                    recovery_kernel_boot_identity(intent.deadline_at)
                    != previous.kernel_boot_identity
                ):
                    raise UpgradeUnavailable(
                        "Changed kernel needs separate supported Source recovery"
                    )
                if fixed_launchctl_probe(intent.deadline_at)(service.target) is not None:
                    raise UpgradeUnavailable("Legacy service loaded during succession observation")
                if (
                    source_identity(settings, previous.consumption)
                    != previous.legacy_stop.preserved_source
                ):
                    raise UpgradeUnavailable("Succession Source preservation differs")
            if control.verify_recovery_owner(previous).sha256() != previous.sha256():
                raise UpgradeUnavailable("Succession predecessor revoked before observation")
            # Core I/O can yield while native facts change. Sample again after it,
            # retaining the beginning (not the end) of this bounded physical window.
            sampled_at = datetime.now(UTC)
            sample_started = time.monotonic()
            sample_deadline = min(intent.deadline_at, sampled_at + timedelta(seconds=5))
            if verify_passive_support_identity(settings, intent, previous.runtime) != successor:
                raise UpgradeUnavailable("Succession final support identity changed")
            verify_retired_identity(previous.process)
            verify_retired_identity(previous.consumption.transfer.legacy_guard.process)
            if recovery_kernel_boot_identity(sample_deadline) != previous.kernel_boot_identity:
                raise UpgradeUnavailable("Succession final kernel differs")
            if fixed_launchctl_probe(sample_deadline)(service.target) is not None:
                raise UpgradeUnavailable("Succession final legacy service loaded")
            if (
                source_identity(settings, previous.consumption)
                != previous.legacy_stop.preserved_source
            ):
                raise UpgradeUnavailable("Succession final Source preservation differs")
            if time.monotonic() - sample_started > 5 or datetime.now(UTC) >= sample_deadline:
                raise UpgradeUnavailable("Succession physical observation budget exhausted")
            return UpgradeExpiredRecoverySuccessionObservation(
                succession_id=succession_id,
                previous_owner=previous,
                successor=successor,
                kernel_boot_identity=previous.kernel_boot_identity,
                preserved_source=previous.legacy_stop.preserved_source,
                observed_at=sampled_at,
                previous_owner_retired=True,
            )
