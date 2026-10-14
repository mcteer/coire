"""Successor admission requires positive prior completion and exact executor provenance."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from test_handover_creation_journal import preparation

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import (
    BootstrapHandover,
    CoreHandoverSummary,
    CoreHandoverUnchangedIntent,
    DeploymentHandoverHold,
    DeploymentOwnership,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.bootstrap_admission import verify_bootstrap_admission
from coire_upgrade_executor.handover_creation import HandoverCreationJournal


@pytest.mark.parametrize(
    "fault", [None, "missing_ack", "foreign_ack", "image", "provenance", "dispatch", "epoch"]
)
def test_successor_to_unchanged_failure_preserves_exact_source(
    tmp_path: Path, fault: str | None
) -> None:
    prep = preparation()
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    bootstrap.write("handover-preparation.json", prep)
    creation = HandoverCreationJournal(bootstrap.root / "handover/2", prep)
    effect = creation.begin(now=prep.prepared_at)
    if fault == "dispatch":
        creation.dispatch(effect, now=prep.prepared_at)
    creation.store.write(
        "failure-intent.json",
        CoreHandoverUnchangedIntent(
            preparation_sha256=prep.sha256(),
            incumbent_seed_sha256=prep.incumbent.sha256(),
            generation=prep.generation,
            intent_at=prep.prepared_at,
        ),
    )
    ack = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=prep.generation + (1 if fault == "epoch" else 0),
        phase="failed_unchanged",
        preparation_sha256="f" * 64 if fault == "foreign_ack" else prep.sha256(),
        candidate_release_id=prep.candidate_release.release_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_image_id=prep.candidate_image_id,
        prepared_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    if fault != "missing_ack":
        creation.store.write("failure-ack.json", ack)
    seed = prep.incumbent
    if fault == "image":
        seed = seed.model_copy(update={"executor_image_id": "sha256:" + "a" * 64})
    elif fault == "provenance":
        seed = seed.model_copy(update={"executor_release": prep.candidate_release})
    store = PrivateUpgradeStore(tmp_path / "coordination")
    hold = DeploymentHandoverHold(
        project="coire",
        preparation_sha256="b" * 64,
        generation=prep.generation + 1,
        incumbent_runtime_id=seed.executor_runtime_id,
        incumbent_container_id=seed.executor_container_id,
        candidate_runtime_id="successor-executor",
        updated_at=prep.prepared_at,
    )
    store.write("handover-hold.json", hold)
    previous = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="coire-upgrade-executor",
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id=seed.executor_container_id,
        boot_identity="isolated-test-boot",
        generation=prep.generation,
        source_sha256=seed.source.manifest_sha256,
        stage="preparing",
    )
    before = {
        path.name: path.read_bytes() for path in creation.store.root.iterdir() if path.is_file()
    }
    args = (store, "coire", previous.owner_identity, seed, previous, None, hold, bootstrap.root)
    if fault:
        with pytest.raises(UpgradeConflict):
            verify_bootstrap_admission(*args)
    else:
        verify_bootstrap_admission(*args)
    assert store.read("handover-hold.json", DeploymentHandoverHold) == hold
    assert not (bootstrap.root / "handover/3").exists()
    assert before == {
        path.name: path.read_bytes() for path in creation.store.root.iterdir() if path.is_file()
    }


@pytest.mark.parametrize("phase", ["committed", "restored"])
@pytest.mark.parametrize("fault", [None, "missing_ack", "generation", "selected", "phase"])
def test_successor_preserves_the_acknowledged_prior_selected_executor(
    tmp_path: Path, phase: Literal["committed", "restored"], fault: str | None
) -> None:
    prep = preparation()
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    bootstrap.write("handover-preparation.json", prep)
    old = PrivateUpgradeStore(bootstrap.root / "handover/2")
    current = BootstrapHandover(
        incumbent_id=prep.incumbent.executor_runtime_id,
        candidate_id=prep.candidate_runtime_id,
        incumbent_container_id=prep.incumbent.executor_container_id,
        candidate_container_id="a" * 64,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_image_id=prep.candidate_image_id,
        generation=prep.generation + (1 if fault == "generation" else 0),
        phase=phase,
        deadline_at=prep.deadline_at,
        updated_at=prep.deadline_at,
        stability_started_at=prep.prepared_at if phase == "committed" else None,
    )
    bootstrap.write("bootstrap-handover.json", current)
    ack = CoreHandoverSummary(
        handover_id=uuid4(),
        project="coire",
        generation=prep.generation,
        phase="stable" if fault == "phase" else phase,
        preparation_sha256=prep.sha256(),
        candidate_release_id=prep.candidate_release.release_id,
        incumbent_image_id=prep.incumbent.executor_image_id,
        candidate_image_id=prep.candidate_image_id,
        prepared_at=prep.prepared_at,
        deadline_at=prep.deadline_at,
    )
    if fault != "missing_ack":
        old.write("commit-ack.json" if phase == "committed" else "restoration-ack.json", ack)
    seed = prep.incumbent
    if phase == "committed":
        seed = seed.model_copy(
            update={
                "executor_runtime_id": current.candidate_id,
                "executor_container_id": current.candidate_container_id,
                "executor_image_id": current.candidate_image_id,
                "executor_release": prep.candidate_release,
            }
        )
    if fault == "selected":
        seed = seed.model_copy(update={"executor_container_id": "b" * 64})
    store = PrivateUpgradeStore(tmp_path / "coordination")
    hold = DeploymentHandoverHold(
        project="coire",
        preparation_sha256="c" * 64,
        generation=3,
        incumbent_runtime_id=seed.executor_runtime_id,
        incumbent_container_id=seed.executor_container_id,
        candidate_runtime_id="successor",
        updated_at=prep.deadline_at,
    )
    store.write("handover-hold.json", hold)
    previous = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="coire-upgrade-executor",
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id=seed.executor_container_id,
        boot_identity="isolated-test-boot",
        generation=3,
        source_sha256=seed.source.manifest_sha256,
        stage="preparing",
    )
    args = (store, "coire", previous.owner_identity, seed, previous, None, hold, bootstrap.root)
    if fault:
        with pytest.raises(UpgradeConflict):
            verify_bootstrap_admission(*args)
    else:
        verify_bootstrap_admission(*args)
    assert bootstrap.read("bootstrap-handover.json", BootstrapHandover) == current
    assert not (bootstrap.root / "handover/3").exists()
