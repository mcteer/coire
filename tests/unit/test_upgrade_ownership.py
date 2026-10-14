"""Host and container ownership never treats a dead foreign PID as permission."""

from pathlib import Path

import pytest

from coire_core.deploy import deployment_ownership
from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import DeploymentOwnership
from coire_core.upgrade_journal import PrivateUpgradeStore


def owner(generation: int, kind: str = "host") -> DeploymentOwnership:
    return DeploymentOwnership.model_validate(
        {
            "project": "coire",
            "owner_kind": kind,
            "owner_identity": f"owner-{generation}",
            "boot_identity": "boot-one",
            "generation": generation,
        }
    )


def test_live_owner_excludes_other_namespace(tmp_path: Path) -> None:
    with (
        deployment_ownership(tmp_path / "coordination", owner(1)),
        pytest.raises(UpgradeConflict),
        deployment_ownership(tmp_path / "coordination", owner(2, "executor")),
    ):
        pass
    with deployment_ownership(tmp_path / "coordination", owner(2, "executor")):
        pass


def test_abandoned_executor_requires_exact_reconciliation_witness(tmp_path: Path) -> None:
    root = tmp_path / "coordination"
    with pytest.raises(RuntimeError), deployment_ownership(root, owner(1, "executor")):
        raise RuntimeError("host interruption")
    with pytest.raises(UpgradeConflict), deployment_ownership(root, owner(2)):
        pass
    previous = PrivateUpgradeStore(root).read("ownership.json", DeploymentOwnership)
    assert previous is not None and previous.state == "active"
    with (
        pytest.raises(UpgradeConflict),
        deployment_ownership(
            root,
            owner(2),
            reconciled_prior=previous.model_copy(update={"boot_identity": "other-boot"}),
        ),
    ):
        pass
    with deployment_ownership(root, owner(2), reconciled_prior=previous):
        pass
    committed = PrivateUpgradeStore(root).read("ownership.json", DeploymentOwnership)
    assert committed is not None and committed.state == "committed"


def test_stale_generation_never_reacquires(tmp_path: Path) -> None:
    root = tmp_path / "coordination"
    with deployment_ownership(root, owner(2)):
        pass
    with pytest.raises(UpgradeConflict), deployment_ownership(root, owner(1)):
        pass


def test_host_command_holds_inherited_fd_and_excludes_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from coire_core.deploy import run_host_deployment, verify_host_deployment

    source = tmp_path / "source"
    script = source / "deploy/compose/coire-up"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    state = tmp_path / "state"
    coordination = state / "coire/upgrades/coordination"
    monkeypatch.setattr("coire_core.deploy.host_boot_identity", lambda: "boot-one")

    def execute(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv == ["bash", str(script), "--no-build"]
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        with monkeypatch.context() as nested:
            for key, value in environment.items():
                nested.setenv(key, value)
            verify_host_deployment(state, "coire")
        with (
            pytest.raises(UpgradeConflict),
            deployment_ownership(coordination, owner(2, "executor")),
        ):
            pass
        assert kwargs["pass_fds"] == (int(environment["COIRE_DEPLOY_OWNER_FD"]),)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", execute)
    assert (
        run_host_deployment(
            "up", ("--no-build",), source_root=source, state_root=state, project="coire"
        )
        == 0
    )
    committed = PrivateUpgradeStore(coordination).read("ownership.json", DeploymentOwnership)
    assert committed is not None and committed.state == "committed"


@pytest.mark.parametrize("mutated", [False, True])
def test_failed_host_command_retains_only_unreconciled_runtime_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutated: bool
) -> None:
    import subprocess

    from coire_core.deploy import mark_host_mutation, run_host_deployment

    state = tmp_path / "state"
    source = tmp_path / "source"
    monkeypatch.setattr("coire_core.deploy.host_boot_identity", lambda: "boot-one")

    def execute(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        with monkeypatch.context() as nested:
            for key, value in environment.items():
                nested.setenv(key, value)
            if mutated:
                mark_host_mutation(state, "coire")
        return subprocess.CompletedProcess(argv, 2)

    monkeypatch.setattr(subprocess, "run", execute)
    assert run_host_deployment("up", (), source_root=source, state_root=state, project="coire") == 2
    saved = PrivateUpgradeStore(state / "coire/upgrades/coordination").read(
        "ownership.json", DeploymentOwnership
    )
    assert saved is not None and saved.state == ("active" if mutated else "committed")
    if mutated:
        with pytest.raises(UpgradeConflict):
            run_host_deployment("down", (), source_root=source, state_root=state, project="coire")
    else:
        assert (
            run_host_deployment("up", (), source_root=source, state_root=state, project="coire")
            == 2
        )


def test_old_pid_lock_cannot_authorize_cross_namespace_recovery(tmp_path: Path) -> None:
    from coire_core.deploy import ReleaseInputError, run_host_deployment

    old = tmp_path / "state/coire/.coire-up.lock"
    old.mkdir(parents=True)
    (old / "pid").write_text("99999999")
    with pytest.raises(ReleaseInputError, match="legacy deployment"):
        run_host_deployment(
            "up", (), source_root=tmp_path, state_root=tmp_path / "state", project="coire"
        )
    assert (old / "pid").read_text() == "99999999"


def test_selected_receipt_repairs_host_pointer_and_rejects_lost_source(
    tmp_path: Path,
) -> None:
    import hashlib
    from datetime import UTC, datetime

    from coire_core.deploy import ReleaseInputError, reconcile_release_selection
    from coire_core.models.upgrades import DeploymentSelectionReceipt

    state = tmp_path / "state"
    releases = state / "coire/releases"
    source = releases / "release-source"
    selected = releases / "release-selected"
    for release in (source, selected):
        release.mkdir(parents=True)
        (release / "compose.json").write_bytes(b"{}")
    current = state / "coire/current"
    current.symlink_to(source)
    store = PrivateUpgradeStore(state / "coire/upgrades/coordination")
    receipt = DeploymentSelectionReceipt(
        project="coire",
        owner_identity="executor-one",
        boot_identity="boot-one",
        generation=2,
        release_key=selected.name,
        compose_sha256=hashlib.sha256(b"{}").hexdigest(),
        protected_release_keys=(source.name, selected.name),
        protected_compose_sha256s={
            key: hashlib.sha256(b"{}").hexdigest() for key in (source.name, selected.name)
        },
        selected_at=datetime.now(UTC),
    )
    store.write("selection.json", receipt)
    reconcile_release_selection(state, "coire", store)
    assert current.resolve() == selected
    (source / "compose.json").unlink()
    source.rmdir()
    with pytest.raises(ReleaseInputError, match="retained"):
        reconcile_release_selection(state, "coire", store)
    assert current.resolve() == selected


def test_foreign_kernel_claim_cannot_be_recovered_by_matching_witness(
    tmp_path: Path,
) -> None:
    import sys

    from coire_core.models.upgrades import DeploymentLease

    root = tmp_path / "coordination"
    with pytest.raises(RuntimeError), deployment_ownership(root, owner(1, "executor")):
        raise RuntimeError("interruption")
    store = PrivateUpgradeStore(root)
    lease = store.read("deployment.lease", DeploymentLease)
    previous = store.read("ownership.json", DeploymentOwnership)
    assert lease is not None and previous is not None
    foreign = lease.model_copy(
        update={"kernel_platform": "linux" if sys.platform == "darwin" else "darwin"}
    )
    store.write("deployment.lease", foreign)
    with (
        pytest.raises(UpgradeConflict, match="namespace recovery"),
        deployment_ownership(root, owner(2), reconciled_prior=previous),
    ):
        pass
    assert store.read("deployment.lease", DeploymentLease) == foreign
    assert store.read("ownership.json", DeploymentOwnership) == previous


def test_active_intent_keeps_shared_claim_and_commit_removes_it(tmp_path: Path) -> None:
    root = tmp_path / "coordination"
    with pytest.raises(RuntimeError), deployment_ownership(root, owner(1)):
        raise RuntimeError("interruption")
    store = PrivateUpgradeStore(root)
    assert store.path("deployment.lease").exists()
    previous = store.read("ownership.json", DeploymentOwnership)
    with deployment_ownership(root, owner(2), reconciled_prior=previous):
        assert store.path("deployment.lease").exists()
    assert not store.path("deployment.lease").exists()


def test_changed_owner_cannot_be_committed_on_context_exit(tmp_path: Path) -> None:
    root = tmp_path / "coordination"
    with (
        pytest.raises(UpgradeConflict, match="changed during execution"),
        deployment_ownership(root, owner(1)) as store,
    ):
        store.write("ownership.json", owner(2, "executor"))
    store = PrivateUpgradeStore(root)
    assert store.read("ownership.json", DeploymentOwnership) == owner(2, "executor")
    assert store.path("deployment.lease").exists()
