"""Fixed retained predecessor reads, not portable ownership or PID discovery."""

from pathlib import Path

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdClaim,
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryColdSourceProcess,
)
from coire_core.models.upgrade_recovery_cold_binding import UpgradeColdContinuationBinding
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


def previous_source(
    settings: Settings, binding: UpgradeColdContinuationBinding
) -> tuple[UpgradeExpiredRecoveryColdClaim, UpgradeExpiredRecoveryColdSourceProcess]:
    root = PrivateUpgradeStore(Path(settings.upgrade_bootstrap_root), create=False)
    runs = PrivateUpgradeStore(root.path("expired-recovery"), create=False)
    run = PrivateUpgradeStore(runs.path(binding.previous_recovery_id), create=False)
    owner = run.read("cold.claim.json", UpgradeExpiredRecoveryColdClaim)
    source = run.read("cold.source.process.0.json", UpgradeExpiredRecoveryColdSourceProcess)
    if (
        owner is None
        or source is None
        or owner.sha256() != binding.previous_owner_sha256
        or owner.intent.sha256() != binding.previous_intent_sha256
        or owner.intent.recovery_id != binding.previous_recovery_id
        or owner.intent.deadline_at != binding.previous_deadline_at
        or owner.original_claim != binding.original_claim
        or owner.original_native != binding.original_native
        or owner.kernel_boot_identity != binding.previous_kernel_boot_identity
        or owner.retired_supervisor is None
        or owner.retired_supervisor.sha256() != binding.retired_supervisor_sha256
        or source.sha256() != binding.previous_source_sha256
        or source.launch.source.projection.owner != owner
        or source.launch.source.projection.projected_native != binding.projected_native
        or source.process != binding.retained_node.process
        or source.launch.source.projection.source_selection != binding.retained_node.selected
    ):
        raise UpgradeConflict("Cold continuation retained predecessor or Source differs")
    if binding.previous_archive_sha256 is not None:
        from coire_node.expired_recovery_continuation_history import (
            retained_archive_boundary,
            retained_qualification_boundary,
        )

        command = run.read("cold.smoke.command.json", UpgradeExpiredRecoveryColdSmokeCommand)
        archive, smoke, results = retained_archive_boundary(run, source, command)
        if (
            archive is None
            or archive.sha256() != binding.previous_archive_sha256
            or archive.archive.history_sha256 != binding.previous_history_sha256
            or (smoke.sha256() if smoke is not None else None) != binding.previous_smoke_sha256
            or results is None
            or tuple(result.sha256() for result in results) != binding.previous_pair_result_sha256s
            or retained_qualification_boundary(run, smoke, results, archive)
            != (
                binding.previous_pair_qualification_sha256,
                binding.previous_verification_request_sha256,
            )
        ):
            raise UpgradeConflict("Cold continuation retained archive or failed results changed")
    if binding.post_handback is not None and binding.post_handback.stage == "ready_unreceipted":
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdHandbackIntent
        from coire_node.expired_recovery_partial_handback import retained_unreceipted_helper

        late = binding.post_handback
        intent = run.read("cold.handback.intent.json", UpgradeExpiredRecoveryColdHandbackIntent)
        if intent is None or intent.sha256() != late.handback_sha256:
            raise UpgradeConflict("Partial Source changes retained handback intent")
        helper = retained_unreceipted_helper(run, source, intent)
        if (
            helper.sha256() != late.helper_sha256
            or helper.process != late.source_helper.process
            or helper.handback.released_claim != late.released_claim
        ):
            raise UpgradeConflict("Partial Source changes retained helper")
    elif binding.post_handback is not None:
        from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdHandbackReceipt
        from coire_node.expired_recovery_post_handback import retained_post_handback

        handback = run.read("cold.handback.receipt.json", UpgradeExpiredRecoveryColdHandbackReceipt)
        post = retained_post_handback(run, source, handback)
        late = binding.post_handback
        if post is None or (
            post[0].sha256() != late.handback_sha256
            or post[1].sha256() != late.supervision_sha256
            or post[2].sha256() != late.closure_intent_sha256
            or post[0].helper.handback.released_claim != late.released_claim
            or post[0].helper.process != late.source_helper.process
            or post[1].process != late.retired_supervisor
        ):
            raise UpgradeConflict("Cold continuation retained handback preimages changed")
    if binding.untransferred_archive_custodian is not None:
        from coire_node.expired_recovery_archive_custody import pending_archive_custody

        # Metadata stays in its original journal after the bytes move. The
        # observer/archive producers separately pin and verify their current
        # namespace; a moved historical namespace is never inferred as idle.
        if pending_archive_custody(settings, owner, verify_bytes=False) != (
            binding.untransferred_archive_custodian,
            binding.untransferred_archive_sha256,
            binding.untransferred_history_sha256,
        ):
            raise UpgradeConflict("Cold continuation pending archive custody changed")
    return owner, source


def fixture_origin(
    settings: Settings, owner: UpgradeExpiredRecoveryColdClaim
) -> UpgradeExpiredRecoveryColdClaim:
    """Read at most sixteen immutable links to the original fixture provenance."""
    seen: set[str] = set()
    for _ in range(16):
        if owner.intent.recovery_id in seen:
            raise UpgradeConflict("Cold continuation predecessor chain repeats")
        seen.add(owner.intent.recovery_id)
        if owner.continuation is None:
            return owner
        owner, _source = previous_source(settings, owner.continuation)
    raise UpgradeUnavailable("Cold continuation predecessor chain exceeds bound")
