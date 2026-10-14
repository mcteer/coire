"""First passive succession session; not an installer or legacy-mode fallback."""

import threading
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
from coire_node.expired_recovery_succession_wait import wait_for_succession_grant
from coire_node.expired_recovery_successor_controller import run_successor_holding
from coire_node.upgrade_guard import NativeGuardTransport

tracer = trace.get_tracer("coire.node.recovery_successor_session")


@tracer.start_as_current_span("coire.node.recovery_successor_session_initial")
def run_initial_successor_session(
    settings: Settings,
    previous: UpgradeExpiredRecoveryOwnerClaim,
    succession_id: str,
    control: NativeGuardTransport,
    stopped: threading.Event,
) -> None:
    """Compose passive delivery and actual-exclusion controller, without local grants.

    Existing claims require explicit configured resume, never a fresh claim epoch.
    No installer/startup selector invokes this until trusted acquisition is complete.
    Controller owns initial claim persistence, physical gates and lifetime lease.
    """
    if stopped.is_set():
        return
    previous = UpgradeExpiredRecoveryOwnerClaim.model_validate_json(previous.model_dump_json())
    intent = previous.consumption.transfer.intent
    if not settings.upgrades_enabled or settings.node_name != intent.original_plan.operation.node:
        raise UpgradeUnavailable("Initial successor session node is unauthorized")
    if datetime.now(UTC) >= intent.deadline_at:
        raise UpgradeUnavailable("Initial successor original budget expired")
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(intent.recovery_id), create=False)
    if run.read("owner.claim.json", UpgradeExpiredRecoveryOwnerClaim) != previous:
        raise UpgradeConflict("Initial successor predecessor differs")
    # Validate the identifier before using it in a private path or any delivery I/O.
    from pydantic import TypeAdapter

    from coire_core.models.upgrades import UpgradeId

    succession_id = TypeAdapter(UpgradeId).validate_python(succession_id)
    if (
        run.read(f"successor.{succession_id}.claim.json", UpgradeExpiredRecoverySuccessorClaim)
        is not None
    ):
        raise UpgradeUnavailable("Retained successor claim requires explicit resume")
    grant = wait_for_succession_grant(settings, previous, succession_id, control, stopped)
    if stopped.is_set() or grant is None:
        return
    if (
        grant.observation.previous_owner != previous
        or grant.observation.succession_id != succession_id
    ):
        raise UpgradeConflict("Initial successor delivery scope differs")
    now = datetime.now(UTC)
    if now >= min(intent.deadline_at, grant.expires_at):
        raise UpgradeUnavailable("Initial successor handover budget expired")
    claim = UpgradeExpiredRecoverySuccessorClaim(
        grant=grant,
        process=grant.observation.successor,
        runtime=previous.runtime,
        kernel_boot_identity=grant.observation.kernel_boot_identity,
        preserved_source=grant.observation.preserved_source,
        claimed_at=now,
    )
    run_successor_holding(settings, claim, control, stopped)
