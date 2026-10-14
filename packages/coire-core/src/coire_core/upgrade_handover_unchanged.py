"""Shared positive no-dispatch evidence for effect-free executor handover failure."""

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreHandoverCreation,
    CoreHandoverPreparation,
    CoreHandoverUnchangedIntent,
)
from coire_core.upgrade_journal import PrivateUpgradeStore


def verify_unchanged_files(
    bootstrap: PrivateUpgradeStore,
    journal: PrivateUpgradeStore,
    preparation: CoreHandoverPreparation,
) -> CoreHandoverUnchangedIntent:
    intent = journal.read("failure-intent.json", CoreHandoverUnchangedIntent)
    created = journal.read("creation.json", CoreHandoverCreation)
    if (
        intent is None
        or (intent.preparation_sha256, intent.incumbent_seed_sha256, intent.generation)
        != (preparation.sha256(), preparation.incumbent.sha256(), preparation.generation)
        or intent.intent_at < preparation.prepared_at
        or bootstrap.read("handover-preparation.json", CoreHandoverPreparation) != preparation
        or journal.read("preparation.json", CoreHandoverPreparation) != preparation
        or (
            created is not None
            and (
                created.outcome != "intent"
                or created.preparation_sha256 != preparation.sha256()
                or created.generation != preparation.generation
                or created.candidate_name != preparation.candidate_name
                or created.candidate_image_id != preparation.candidate_image_id
            )
        )
    ):
        raise UpgradeConflict("Effect-free failure has no exact durable no-dispatch intent")
    for key in (
        "candidate-seed.json",
        "candidate-start.json",
        "passive-proof.json",
        "transfer-intent.json",
        "commit-proof.json",
        "commit-decision.json",
        "recovery-intent.json",
        "retirement-intent.json",
    ):
        path = journal.path(key)
        if path.exists() or path.is_symlink():
            raise UpgradeConflict("Effect-free failure retains candidate effect evidence")
    current = bootstrap.read("bootstrap-handover.json", BootstrapHandover)
    if current is not None:
        selected = (
            (current.candidate_id, current.candidate_container_id, current.candidate_image_id)
            if current.phase == "committed"
            else (current.incumbent_id, current.incumbent_container_id, current.incumbent_image_id)
        )
        if (
            current.phase not in {"committed", "restored"}
            or current.generation >= preparation.generation
            or selected
            != (
                preparation.incumbent.executor_runtime_id,
                preparation.incumbent.executor_container_id,
                preparation.incumbent.executor_image_id,
            )
        ):
            raise UpgradeConflict("Effect-free failure conflicts with retained recovery ownership")
    return intent
