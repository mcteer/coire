"""Read completed historical boundaries without granting qualification to a new intent."""

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryColdCollectiveArchive,
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt,
    UpgradeExpiredRecoveryColdPairCommand,
    UpgradeExpiredRecoveryColdPairQualification,
    UpgradeExpiredRecoveryColdPairResult,
    UpgradeExpiredRecoveryColdSmokeCommand,
    UpgradeExpiredRecoveryColdSmokeReceipt,
    UpgradeExpiredRecoveryColdSourceProcess,
    UpgradeExpiredRecoveryColdVerificationRequest,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.expired_recovery_cold_archive import verify_archive_cleanup


def retained_archive_boundary(
    run: PrivateUpgradeStore,
    source: UpgradeExpiredRecoveryColdSourceProcess,
    core_smoke: UpgradeExpiredRecoveryColdSmokeCommand | None,
) -> tuple[
    UpgradeExpiredRecoveryColdCollectiveArchiveReceipt | None,
    UpgradeExpiredRecoveryColdSmokeReceipt | None,
    tuple[UpgradeExpiredRecoveryColdPairResult, ...] | None,
]:
    archive = run.read(
        "cold.collective.archive.receipt.json", UpgradeExpiredRecoveryColdCollectiveArchiveReceipt
    )
    smoke = run.read("cold.smoke.receipt.json", UpgradeExpiredRecoveryColdSmokeReceipt)
    command = run.read("cold.smoke.command.json", UpgradeExpiredRecoveryColdSmokeCommand)
    if archive is None:
        if (
            any(
                run.path(name).exists() or run.path(name).is_symlink()
                for name in (
                    "cold.collective.archive.intent.json",
                    "cold.collective.archive.effect.json",
                    "cold.smoke.command.json",
                    "cold.smoke.receipt.json",
                )
            )
            or core_smoke is not None
        ):
            raise UpgradeUnavailable("Cold continuation archive or smoke outcome is uncertain")
        return None, None, None
    initial = run.read(
        "cold.collective.archive.intent.json", UpgradeExpiredRecoveryColdCollectiveArchive
    )
    effect = run.read(
        "cold.collective.archive.effect.json", UpgradeExpiredRecoveryColdCollectiveArchive
    )
    if (
        initial is None
        or archive.initial_archive_sha256 != initial.sha256()
        or archive.archive != effect
        or archive.archive.source != source
    ):
        raise UpgradeConflict("Cold continuation archive has another retained provenance")
    verify_archive_cleanup(initial)
    verify_archive_cleanup(archive.archive)
    if (smoke is None) != (command is None) or (command is None) != (core_smoke is None):
        raise UpgradeUnavailable("Cold continuation smoke outcome is uncertain")
    if smoke is not None and (
        smoke.smoke != command or command != core_smoke or smoke.smoke.source != source
    ):
        raise UpgradeConflict("Cold continuation smoke has another retained provenance")
    results = []
    missing = False
    for index in range(3):
        dispatch = run.read(
            f"cold.pair.{index}.command.json", UpgradeExpiredRecoveryColdPairCommand
        )
        result = run.read(f"cold.pair.{index}.result.json", UpgradeExpiredRecoveryColdPairResult)
        if dispatch is None and result is None:
            missing = True
            continue
        if missing or dispatch is None or result is None:
            raise UpgradeUnavailable("Cold continuation retains an unresolved paired dispatch")
        if (
            result.dispatch != dispatch
            or dispatch.index != index
            or dispatch.archive != archive
            or dispatch.smoke != smoke
        ):
            raise UpgradeConflict("Cold continuation failed-pair boundary differs")
        results.append(result)
    retained_qualification_boundary(run, smoke, tuple(results), archive)
    return archive, smoke, tuple(results)


def retained_qualification_boundary(
    run: PrivateUpgradeStore,
    smoke: UpgradeExpiredRecoveryColdSmokeReceipt | None,
    results: tuple[UpgradeExpiredRecoveryColdPairResult, ...],
    archive: UpgradeExpiredRecoveryColdCollectiveArchiveReceipt | None = None,
) -> tuple[str | None, str | None]:
    """Bind old receipts only as history; committed Core verification still refuses.

    A pending request is metadata, not proof that Core accepted verification.
    The observer's authenticated Core context independently requires that no
    verification or handback has been committed.
    """
    qualification = run.read(
        "cold.pair.qualification.json", UpgradeExpiredRecoveryColdPairQualification
    )
    request = run.read(
        "cold.verification.request.json", UpgradeExpiredRecoveryColdVerificationRequest
    )
    if qualification is not None and qualification.results != results:
        raise UpgradeConflict("Cold continuation historical qualification results changed")
    if request is not None and (
        request.smoke != smoke
        or request.pair != qualification
        or request.archive != archive
        or (
            qualification is None
            and (
                request.smoke.smoke.source.launch.source.projection.owner.intent.original_plan.source.manifest.paired_required
                or request.smoke.smoke.source.launch.source.projection.owner.intent.original_plan.target.manifest.paired_required
            )
        )
    ):
        raise UpgradeConflict("Cold continuation pending verification provenance differs")
    return (
        qualification.sha256() if qualification is not None else None,
        request.sha256() if request is not None else None,
    )
