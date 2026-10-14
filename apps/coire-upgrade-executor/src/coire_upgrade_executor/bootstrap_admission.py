"""Effect-free bootstrap admission preserves unclaimed maintenance and handover holds."""

from pathlib import Path

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreBootstrapSeed,
    CoreHandoverPreparation,
    CoreHandoverSummary,
    DeploymentHandoverHold,
    DeploymentOperationHold,
    DeploymentOwnership,
)
from coire_core.upgrade_handover_unchanged import verify_unchanged_files
from coire_core.upgrade_journal import PrivateUpgradeStore


def verify_bootstrap_admission(
    store: PrivateUpgradeStore,
    project: str,
    executor_id: str,
    seed: CoreBootstrapSeed,
    previous: DeploymentOwnership | None,
    admission_hold: DeploymentOperationHold | None,
    handover_admission: DeploymentHandoverHold | None,
    bootstrap_root: Path | None,
) -> None:
    """Grant no effects; retain all original epochs and acknowledged predecessor history."""
    if handover_admission is not None:
        if (
            admission_hold is not None
            or bootstrap_root is None
            or not bootstrap_root.exists()
            or handover_admission.released
            or handover_admission.project != project
            or store.read("handover-hold.json", DeploymentHandoverHold) != handover_admission
            or (handover_admission.incumbent_runtime_id, handover_admission.incumbent_container_id)
            != (seed.executor_runtime_id, seed.executor_container_id)
            or previous is None
            or previous.owner_kind != "executor"
            or previous.job_id is not None
            or previous.owner_identity != executor_id
            or previous.stage != "preparing"
            or previous.source_sha256 != seed.source.manifest_sha256
            or (previous.executor_runtime_id, previous.executor_container_id)
            != (seed.executor_runtime_id, seed.executor_container_id)
        ):
            raise UpgradeConflict("Handover admission recovery lacks its unchanged idle Source")
        bootstrap = PrivateUpgradeStore(bootstrap_root)
        preparation = bootstrap.read("handover-preparation.json", CoreHandoverPreparation)
        journal_root = bootstrap_root / "handover" / str(handover_admission.generation)
        if preparation is None or preparation.generation < handover_admission.generation:
            if journal_root.exists() or journal_root.is_symlink():
                raise UpgradeConflict("Unclaimed handover admission has retained candidate history")
            if preparation is not None:
                old_root = bootstrap_root / "handover" / str(preparation.generation)
                if not old_root.exists():
                    raise UpgradeConflict("Unclaimed successor lacks prior handover acknowledgment")
                old = PrivateUpgradeStore(old_root)
                unchanged = old.read("failure-ack.json", CoreHandoverSummary)
                ack: CoreHandoverSummary | None
                if unchanged is not None:
                    verify_unchanged_files(bootstrap, old, preparation)
                    if (
                        seed.executor_runtime_id,
                        seed.executor_container_id,
                        seed.executor_image_id,
                        seed.executor_source,
                    ) != (
                        preparation.incumbent.executor_runtime_id,
                        preparation.incumbent.executor_container_id,
                        preparation.incumbent.executor_image_id,
                        preparation.incumbent.executor_source,
                    ):
                        raise UpgradeConflict("Unclaimed successor differs from unchanged Source")
                    expected_phase = "failed_unchanged"
                    ack = unchanged
                else:
                    current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
                    if current is None or current.phase not in {"committed", "restored"}:
                        raise UpgradeConflict("Prior handover still retains recovery authority")
                    if current.generation != preparation.generation:
                        raise UpgradeConflict(
                            "Prior handover acknowledgment belongs to another epoch"
                        )
                    expected_phase = current.phase
                    ack = old.read(
                        "commit-ack.json"
                        if expected_phase == "committed"
                        else "restoration-ack.json",
                        CoreHandoverSummary,
                    )
                    selected = (
                        (
                            current.candidate_id,
                            current.candidate_container_id,
                            current.candidate_image_id,
                        )
                        if expected_phase == "committed"
                        else (
                            current.incumbent_id,
                            current.incumbent_container_id,
                            current.incumbent_image_id,
                        )
                    )
                    if selected != (
                        seed.executor_runtime_id,
                        seed.executor_container_id,
                        seed.executor_image_id,
                    ):
                        raise UpgradeConflict(
                            "Unclaimed successor differs from prior selected executor"
                        )
                if (
                    ack is None
                    or ack.phase != expected_phase
                    or ack.project != project
                    or ack.preparation_sha256 != preparation.sha256()
                    or ack.generation != preparation.generation
                    or ack.candidate_release_id != preparation.candidate_release.release_id
                    or ack.incumbent_image_id != preparation.incumbent.executor_image_id
                    or ack.candidate_image_id != preparation.candidate_image_id
                    or ack.prepared_at != preparation.prepared_at
                    or ack.deadline_at != preparation.deadline_at
                ):
                    raise UpgradeConflict(
                        "Unclaimed successor lacks exact prior terminal acknowledgment"
                    )
        else:
            if (
                preparation.sha256() != handover_admission.preparation_sha256
                or preparation.generation != handover_admission.generation
                or not journal_root.exists()
            ):
                raise UpgradeConflict("Handover admission has another retained preparation")
            verify_unchanged_files(bootstrap, PrivateUpgradeStore(journal_root), preparation)
    if admission_hold is not None and (
        admission_hold.released
        or admission_hold.project != project
        or admission_hold.source_sha256 != seed.source.manifest_sha256
        or store.read("operation-hold.json", DeploymentOperationHold) != admission_hold
        or previous is None
        or previous.owner_kind != "executor"
        or previous.job_id is not None
        or previous.owner_identity != executor_id
        or previous.stage != "preparing"
        or previous.source_sha256 != seed.source.manifest_sha256
        or (previous.executor_runtime_id, previous.executor_container_id)
        != (seed.executor_runtime_id, seed.executor_container_id)
    ):
        raise UpgradeConflict("Bootstrap admission recovery differs from its effect-free Source")
