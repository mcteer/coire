"""Initial bootstrap needs a fresh complete physical proof and a real writer lock."""

from datetime import timedelta
from pathlib import Path

import pytest
from upgrade_bootstrap_fixtures import prepared_bootstrap
from upgrade_fixtures import core_plan

from coire_core.deploy import deployment_mutex
from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    DeploymentOwnership,
)
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_upgrade_executor.ownership import bootstrap_ownership


def test_initial_owner_excludes_other_writers_and_releases_only_its_effect_free_claim(
    tmp_path: Path,
) -> None:
    seed, proof = prepared_bootstrap()
    store = PrivateUpgradeStore(tmp_path / "coordination")
    with bootstrap_ownership(store.root, seed, proof, "executor-source") as authority:
        owner = authority.claim
        authority.verify()
        heartbeat = authority.heartbeat(seed, proof)
        assert heartbeat.physical == proof
        assert heartbeat.ownership == owner
        assert owner.job_id is None and owner.operation_generation is None
        assert owner.source_sha256 == seed.source.manifest_sha256
        assert owner.executor_container_id == seed.executor_container_id
        assert store.read("ownership.json", DeploymentOwnership) == owner
        with pytest.raises(UpgradeConflict), deployment_mutex(store, "coire", "another-owner"):
            pytest.fail("another writer acquired the bootstrap lock")
    assert store.read("ownership.json", DeploymentOwnership) == owner.model_copy(
        update={"state": "committed"}
    )
    assert not store.path("deployment.lease").exists()
    with pytest.raises(UpgradeUnavailable):
        authority.heartbeat(seed, proof)


def test_interrupted_bootstrap_requires_exact_physically_reconciled_prior(
    tmp_path: Path,
) -> None:
    seed, proof = prepared_bootstrap()
    store = PrivateUpgradeStore(tmp_path / "coordination")
    with (
        pytest.raises(RuntimeError, match="interrupted"),
        bootstrap_ownership(store.root, seed, proof, "executor-source") as authority,
    ):
        owner = authority.claim
        raise RuntimeError("interrupted")
    assert store.read("ownership.json", DeploymentOwnership) == owner
    assert store.path("deployment.lease").exists()
    with (
        pytest.raises(UpgradeConflict),
        bootstrap_ownership(store.root, seed, proof, "executor-source"),
    ):
        pytest.fail("abandoned bootstrap was treated as readiness")
    with bootstrap_ownership(
        store.root, seed, proof, "executor-source", reconciled_prior=owner
    ) as recovered:
        assert recovered.claim.generation == owner.generation + 1
    assert not store.path("deployment.lease").exists()


@pytest.mark.parametrize("fault", ["stale", "seed", "source", "container", "image"])
def test_initial_bootstrap_refuses_changed_or_stale_proof_before_creating_state(
    tmp_path: Path, fault: str
) -> None:
    seed, proof = prepared_bootstrap()
    mutations: dict[str, dict[str, object]] = {
        "stale": {"observed_at": proof.observed_at - timedelta(seconds=6)},
        "seed": {"seed_sha256": "f" * 64},
        "source": {"source": proof.source.model_copy(update={"source_sha256": "f" * 64})},
        "container": {"executor_container_id": "f" * 64},
        "image": {"executor_image_id": "sha256:" + "f" * 64},
    }
    invalid = proof.model_copy(update=mutations[fault])
    root = tmp_path / "absent"
    with (
        pytest.raises(UpgradeUnavailable),
        bootstrap_ownership(root, seed, invalid, "executor-source"),
    ):
        pytest.fail("invalid bootstrap proof acquired ownership")
    assert not root.exists()


def test_bootstrap_cannot_reconcile_an_active_application_operation(tmp_path: Path) -> None:
    seed, proof = prepared_bootstrap()
    store = PrivateUpgradeStore(tmp_path / "coordination")
    plan = core_plan()
    from coire_core.deploy import host_boot_identity

    prior = DeploymentOwnership(
        project="coire",
        owner_kind="executor",
        owner_identity="executor-source",
        executor_runtime_id=seed.executor_runtime_id,
        executor_container_id=seed.executor_container_id,
        boot_identity=host_boot_identity(),
        generation=1,
        job_id=plan.operation.job_id,
        operation_generation=plan.operation.generation,
        source_sha256=seed.source.manifest_sha256,
    )
    store.write("ownership.json", prior)
    with (
        pytest.raises(UpgradeConflict),
        bootstrap_ownership(store.root, seed, proof, "executor-source", reconciled_prior=prior),
    ):
        pytest.fail("initial bootstrap took application recovery ownership")
    assert store.read("ownership.json", DeploymentOwnership) == prior


@pytest.mark.parametrize(
    "fault", [None, "source", "project", "released", "changed", "owner", "application"]
)
def test_effect_free_bootstrap_can_resume_original_unclaimed_admission_hold(
    tmp_path: Path, fault: str | None
) -> None:
    from coire_core.models.upgrades import DeploymentOperationHold

    plan = core_plan()
    seed, proof = prepared_bootstrap(plan.source)
    store = PrivateUpgradeStore(tmp_path / "coordination")
    with bootstrap_ownership(store.root, seed, proof, "executor-source") as original:
        previous = original.claim.model_copy(update={"state": "committed"})
    hold = DeploymentOperationHold(
        project="foreign" if fault == "project" else "coire",
        job_id=plan.operation.job_id,
        generation=plan.operation.generation,
        plan_sha256=plan.authority_sha256(),
        source_sha256="f" * 64 if fault == "source" else plan.source.manifest_sha256,
        released=fault == "released",
        created_at=plan.operation.accepted_at,
    )
    store.write("operation-hold.json", hold)
    if fault == "owner":
        previous = previous.model_copy(update={"owner_identity": "foreign"})
        store.write("ownership.json", previous)
    elif fault == "application":
        previous = previous.model_copy(
            update={
                "job_id": plan.operation.job_id,
                "operation_generation": plan.operation.generation,
                "stage": "mutating",
            }
        )
        store.write("ownership.json", previous)
    supplied = (
        hold.model_copy(update={"generation": hold.generation + 1}) if fault == "changed" else hold
    )
    before = store.path("ownership.json").read_bytes()
    if fault:
        with (
            pytest.raises(UpgradeConflict),
            bootstrap_ownership(
                store.root, seed, proof, "executor-source", admission_hold=supplied
            ),
        ):
            pytest.fail("foreign or application hold supplied bootstrap authority")
        assert store.path("ownership.json").read_bytes() == before
    else:
        with bootstrap_ownership(
            store.root, seed, proof, "executor-source", admission_hold=supplied
        ) as resumed:
            resumed.verify()
            assert resumed.claim.generation == previous.generation + 1
            assert resumed.claim.job_id is None and resumed.claim.operation_generation is None
            assert resumed.heartbeat(seed, proof).ownership == resumed.claim
            assert store.read("operation-hold.json", DeploymentOperationHold) == hold
            with pytest.raises(UpgradeConflict), deployment_mutex(store, "coire", "host"):
                pytest.fail("host crossed the retained admission fence")
        assert store.read("operation-hold.json", DeploymentOperationHold) == hold
    assert not store.path("deployment.lease").exists()


@pytest.mark.parametrize(
    "fault", [None, "source", "project", "released", "changed", "candidate_history"]
)
def test_unclaimed_handover_hold_allows_only_the_original_effect_free_source(
    tmp_path: Path, fault: str | None
) -> None:
    from coire_core.models.upgrades import DeploymentHandoverHold

    seed, proof = prepared_bootstrap()
    store = PrivateUpgradeStore(tmp_path / "coordination")
    bootstrap = PrivateUpgradeStore(tmp_path / "state/bootstrap")
    bootstrap.write("initial-seed.json", seed)
    with bootstrap_ownership(store.root, seed, proof, "executor-source") as original:
        previous = original.claim.model_copy(update={"state": "committed"})
    hold = DeploymentHandoverHold(
        project="foreign" if fault == "project" else "coire",
        preparation_sha256="a" * 64,
        generation=2,
        incumbent_runtime_id=seed.executor_runtime_id,
        incumbent_container_id="f" * 64 if fault == "source" else seed.executor_container_id,
        candidate_runtime_id="candidate",
        updated_at=proof.observed_at,
        released=fault == "released",
    )
    store.write("handover-hold.json", hold)
    if fault == "candidate_history":
        PrivateUpgradeStore(bootstrap.root / "handover/2")
    supplied = hold.model_copy(update={"generation": 3}) if fault == "changed" else hold
    before = store.path("ownership.json").read_bytes()
    if fault:
        with (
            pytest.raises(UpgradeConflict),
            bootstrap_ownership(
                store.root,
                seed,
                proof,
                "executor-source",
                handover_admission=supplied,
                bootstrap_root=bootstrap.root,
            ),
        ):
            pytest.fail("uncertain or foreign handover supplied idle ownership")
        assert store.path("ownership.json").read_bytes() == before
    else:
        with bootstrap_ownership(
            store.root,
            seed,
            proof,
            "executor-source",
            handover_admission=supplied,
            bootstrap_root=bootstrap.root,
        ) as resumed:
            resumed.verify()
            assert resumed.claim.generation == previous.generation + 1
            assert resumed.claim.job_id is None
            assert not (bootstrap.root / "handover/2").exists()
            assert store.read("handover-hold.json", DeploymentHandoverHold) == hold
            with pytest.raises(UpgradeConflict), deployment_mutex(store, "coire", "host"):
                pytest.fail("host crossed the pending handover hold")
    assert not store.path("deployment.lease").exists()
