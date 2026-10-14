"""Executor ownership survives process loss as intent, without cross-namespace PID guesses."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from upgrade_fixtures import core_plan

from coire_core.deploy import host_boot_identity
from coire_core.errors import UpgradeConflict, UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import (
    CoreRuntimeSelection,
    CoreSelectedService,
    DeploymentOwnership,
    UpgradeDockerContainer,
    UpgradeExecutionPlan,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership


def claim(plan: UpgradeExecutionPlan) -> DeploymentOwnership:
    return DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="executor-source",
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=plan.operation.source_sha256,
    )


def test_kernel_owner_blocks_second_writer_and_retains_abandoned_intent(tmp_path: Path) -> None:
    plan = core_plan()
    owner = claim(plan)
    root = tmp_path / "coordination"
    with executor_ownership(root, owner, plan) as authority:
        authority.verify()
        with pytest.raises(UpgradeConflict), executor_ownership(root, owner, plan):
            pytest.fail("second owner acquired the kernel lock")
    store = PrivateUpgradeStore(root)
    assert store.read("ownership.json", DeploymentOwnership) == owner
    assert store.path("deployment.lease").exists()
    with (
        pytest.raises(UpgradeConflict),
        executor_ownership(root, owner.model_copy(update={"generation": 2}), plan),
    ):
        pytest.fail("abandoned intent was treated as permission")


@pytest.mark.parametrize("fault", [None, "missing", "runtime", "container"])
def test_operation_restart_retains_the_bootstrapped_executor_incarnation(
    tmp_path: Path, fault: str | None
) -> None:
    plan = core_plan()
    original = claim(plan).model_copy(
        update={
            "executor_runtime_id": "verified-executor-runtime",
            "executor_container_id": "c" * 64,
        }
    )
    root = tmp_path / "coordination"
    with executor_ownership(root, original, plan) as authority:
        authority.verify()
    updates: dict[str, object] = {"generation": 2}
    if fault == "missing":
        updates |= {"executor_runtime_id": None, "executor_container_id": None}
    elif fault == "runtime":
        updates["executor_runtime_id"] = "unverified-replacement"
    elif fault == "container":
        updates["executor_container_id"] = "d" * 64
    restarted = DeploymentOwnership.model_validate(original.model_dump() | updates)
    if fault is None:
        with executor_ownership(root, restarted, plan, reconciled_prior=original) as authority:
            authority.verify()
            assert authority.claim.executor_runtime_id == original.executor_runtime_id
    else:
        with (
            pytest.raises(UpgradeConflict, match="incarnation"),
            executor_ownership(root, restarted, plan, reconciled_prior=original),
        ):
            pytest.fail("operation ownership replaced its executor bootstrap")
        assert PrivateUpgradeStore(root).read("ownership.json", DeploymentOwnership) == original


@pytest.mark.parametrize("fault", ["scope", "boot", "source"])
def test_executor_claim_must_bind_exact_operation(tmp_path: Path, fault: str) -> None:
    plan = core_plan()
    owner = claim(plan)
    if fault == "scope":
        owner = owner.model_copy(update={"operation_generation": 2})
    elif fault == "boot":
        owner = owner.model_copy(update={"boot_identity": "different-boot"})
    else:
        owner = owner.model_copy(update={"source_sha256": "f" * 64})
    with (
        pytest.raises(UpgradeForbidden),
        executor_ownership(tmp_path / "coordination", owner, plan),
    ):
        pytest.fail("foreign authority was accepted")
    assert (
        PrivateUpgradeStore(tmp_path / "coordination").read("ownership.json", DeploymentOwnership)
        is None
    )


def test_changed_durable_owner_is_detected_before_an_effect(tmp_path: Path) -> None:
    plan = core_plan()
    owner = claim(plan)
    with executor_ownership(tmp_path / "coordination", owner, plan) as authority:
        authority.store.write(
            "ownership.json", owner.model_copy(update={"owner_identity": "foreign"})
        )
        with pytest.raises(UpgradeConflict):
            authority.verify()
    with pytest.raises(UpgradeUnavailable):
        authority.verify()


def test_reconciled_restart_preserves_first_physical_selection_proof(tmp_path: Path) -> None:
    plan = core_plan()
    owner = claim(plan)
    journal = ExecutorJournal(tmp_path / "journal", plan)
    source = plan.source
    assert source.manifest.compose_sha256 is not None
    assert source.manifest.host_release_key is not None
    now = datetime.now(UTC)
    role = source.manifest.services[0]
    selection = CoreRuntimeSelection(
        project=owner.project,
        owner_identity=owner.owner_identity,
        generation=owner.generation,
        release_id=source.release_id,
        manifest_sha256=source.manifest_sha256,
        compose_sha256=source.manifest.compose_sha256,
        host_release_key=source.manifest.host_release_key,
        selected_at=now,
        services=(
            CoreSelectedService(
                container=UpgradeDockerContainer(
                    container_id="a" * 64,
                    name="coire-coire-api-1",
                    project="coire",
                    service=role.service,
                    image_id=role.image_id,
                    observed_at=now,
                    running=True,
                    health="healthy",
                    exit_code=0,
                    network_ids={"coire-internal": "b" * 64},
                )
            ),
        ),
    )
    root = tmp_path / "coordination"
    with executor_ownership(root, owner, plan) as authority:
        journal.retain_runtime_selection(selection, authority)
    path = journal.store.path("runtime-selection-" + str(source.release_id) + ".json")
    original = path.read_bytes()
    replacement = owner.model_copy(update={"generation": 2})
    with executor_ownership(root, replacement, plan, reconciled_prior=owner) as authority:
        fresh = selection.model_copy(update={"generation": 2})
        assert journal.retain_runtime_selection(fresh, authority) == fresh
        assert path.read_bytes() == original
        changed = fresh.model_copy(
            update={
                "services": (
                    fresh.services[0].model_copy(
                        update={
                            "container": fresh.services[0].container.model_copy(
                                update={"container_id": "c" * 64}
                            )
                        }
                    ),
                )
            }
        )
        with pytest.raises(UpgradeConflict, match="changed on replay"):
            journal.retain_runtime_selection(changed, authority)
