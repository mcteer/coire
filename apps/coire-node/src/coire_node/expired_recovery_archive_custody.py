"""Locate an actual untransferred archive through immutable predecessor records."""

from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def pending_archive_custody(
    settings: Settings, owner: UpgradeExpiredRecoveryColdClaim, *, verify_bytes: bool = True
) -> tuple[str, str, str] | None:
    binding = owner.continuation
    archive_sha: str | None
    if binding is None:
        return None
    if binding.previous_archive_sha256 is not None:
        custodian, archive_sha, history_sha = (
            binding.previous_recovery_id,
            binding.previous_archive_sha256,
            binding.previous_history_sha256,
        )
    elif binding.untransferred_archive_custodian is not None:
        custodian, archive_sha, history_sha = (
            binding.untransferred_archive_custodian,
            binding.untransferred_archive_sha256,
            binding.untransferred_history_sha256,
        )
    else:
        return None
    from coire_node.expired_recovery_cold_archive import history_sha256
    from coire_node.expired_recovery_cold_continuation import previous_source

    current = owner
    seen = {owner.intent.recovery_id}
    for _ in range(16):
        if current.continuation is None:
            raise UpgradeConflict("Pending archive has no immutable predecessor custodian")
        current, _source = previous_source(settings, current.continuation)
        if current.intent.recovery_id in seen:
            raise UpgradeConflict("Pending archive custody repeats")
        seen.add(current.intent.recovery_id)
        if current.intent.recovery_id != custodian:
            continue
        root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
        run = PrivateUpgradeStore(root.path("expired-recovery") / custodian, create=False)
        archive = run.read(
            "cold.collective.archive.receipt.json",
            UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
        )
        if (
            archive is None
            or archive.sha256() != archive_sha
            or archive.archive.history_sha256 != history_sha
            or archive.archive.source.launch.source.projection.owner != current
        ):
            raise UpgradeConflict("Pending archive differs from exact predecessor evidence")
        if verify_bytes:
            history = PrivateUpgradeStore(
                run.path("collective-history") / owner.intent.request.original_job_id, create=False
            )
            if history_sha256(history) != history_sha:
                raise UpgradeConflict("Pending archived bytes changed")
        return custodian, archive.sha256(), archive.archive.history_sha256
    raise UpgradeUnavailable("Pending archive custody traversal exceeds its bound")
