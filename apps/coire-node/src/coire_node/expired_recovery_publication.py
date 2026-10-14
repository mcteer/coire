"""Supported write-ahead unready bootstrap publication, no Source journal edits."""

from datetime import UTC, datetime
from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoveryPublicationIntent,
)
from coire_core.models.upgrades import NativeGuardStatus, NativeUpgradeClaim, PreparedNativeRuntime
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_controller import publish_holding_controller
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport


def publish_unready_support(
    settings: Settings,
    owner: UpgradeExpiredRecoveryOwnerClaim,
    control: NativeGuardTransport,
) -> NativeGuardStatus:
    """Reconcile only recorded old/new preimages with fresh gates before writes.

    Intermediate runtime/status mismatch fails legacy readiness closed. Neither
    Source current link/selection nor original claim/intent/receipts are changed.
    This is not a running recovery controller or serving/cleanup qualification.
    """
    bootstrap = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    require_recovery_lease(bootstrap, owner.sha256())
    publish_holding_controller(settings, owner, control)
    runs = PrivateUpgradeStore(bootstrap.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(
        runs.path(owner.consumption.transfer.intent.recovery_id), create=False
    )
    intent = run.read("publication.intent.json", UpgradeExpiredRecoveryPublicationIntent)
    claim = bootstrap.read("claim.json", NativeUpgradeClaim)
    runtime = bootstrap.read("runtime.json", PreparedNativeRuntime)
    status = bootstrap.read("status.json", NativeGuardStatus)
    if intent is None:
        if claim is None or runtime is None or status is None:
            raise UpgradeUnavailable("Legacy bootstrap publication preimages are absent")
        intent = UpgradeExpiredRecoveryPublicationIntent(
            owner=owner,
            original_claim=claim,
            before_runtime=runtime,
            before_status=status,
            published_status=NativeGuardStatus(
                node=owner.consumption.transfer.legacy_guard.node,
                bootstrap_runtime=owner.runtime,
                process=owner.process,
                selected=owner.legacy_stop.preserved_source.selected,
                ready=False,
                observed_at=datetime.now(UTC),
            ),
        )
        run.write("publication.intent.json", intent, replace=False)
    if intent.owner.sha256() != owner.sha256() or claim != intent.original_claim:
        raise UpgradeConflict("Support bootstrap publication authority changed")
    if runtime not in (intent.before_runtime, owner.runtime) or status not in (
        intent.before_status,
        intent.published_status,
    ):
        raise UpgradeConflict("Support bootstrap publication preimages changed")
    if runtime != owner.runtime and status == intent.published_status:
        raise UpgradeConflict("Support status precedes recorded runtime transition")
    if runtime != owner.runtime:
        publish_holding_controller(settings, owner, control)
        require_recovery_lease(bootstrap, owner.sha256())
        if (
            bootstrap.read("runtime.json", PreparedNativeRuntime) != runtime
            or bootstrap.read("claim.json", NativeUpgradeClaim) != claim
        ):
            raise UpgradeConflict("Legacy bootstrap changed before runtime publication")
        bootstrap.write("runtime.json", owner.runtime)
    if status != intent.published_status:
        publish_holding_controller(settings, owner, control)
        require_recovery_lease(bootstrap, owner.sha256())
        if (
            bootstrap.read("status.json", NativeGuardStatus) != status
            or bootstrap.read("claim.json", NativeUpgradeClaim) != claim
        ):
            raise UpgradeConflict("Legacy bootstrap changed before status publication")
        bootstrap.write("status.json", intent.published_status)
    if (
        bootstrap.read("claim.json", NativeUpgradeClaim) != intent.original_claim
        or bootstrap.read("runtime.json", PreparedNativeRuntime) != owner.runtime
        or bootstrap.read("status.json", NativeGuardStatus) != intent.published_status
    ):
        raise UpgradeConflict("Support bootstrap publication postconditions changed")
    return intent.published_status
