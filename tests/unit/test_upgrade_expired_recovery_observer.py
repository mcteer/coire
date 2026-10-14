"""Passive observer reads retained authority without creating/changing journals."""

from types import SimpleNamespace

import pytest
from test_upgrade_expired_recovery_capture import retained_native_fixture

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NativeUpgradeClaim
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "missing", [False, True, "unknown_slot", "linked_stage", "wrong_interpreter", "torn_claim"]
)
def test_passive_observer_reads_existing_only_and_preserves_preimages(
    tmp_path, monkeypatch, missing
):
    from coire_node import expired_recovery_observer as observer

    plan, native, scope, request, now = retained_native_fixture(tmp_path)
    settings = Settings(
        node_name="coire-edge-a",
        upgrade_environment_root=str(tmp_path / "envs"),
        upgrade_bootstrap_root=str(tmp_path / "bootstrap"),
        node_state_dir=str(tmp_path / "state"),
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "bootstrap")
    claim = NativeUpgradeClaim(
        job_id=native.job_id,
        generation=native.generation,
        plan_sha256=native.plan_sha256,
        node="coire-edge-a",
        updated_at=now,
    )
    bootstrap.write("claim.json", claim)
    operations = PrivateUpgradeStore(bootstrap.root / "operations")
    operation = PrivateUpgradeStore(operations.root / native.job_id)
    operation.write("plan.json", plan)
    guard = PrivateUpgradeStore(operation.root / "guard")
    guard.write("intent.json", native)
    upgrades = PrivateUpgradeStore(tmp_path / "state/upgrades")
    collections = PrivateUpgradeStore(upgrades.root / "collectives")
    job = PrivateUpgradeStore(collections.root / native.job_id)
    stage = PrivateUpgradeStore(job.root / "candidate")
    slot = PrivateUpgradeStore(stage.root / "0")
    slot.write("scope.json", scope)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    if missing is True:
        (guard.root / "intent.json").unlink()
    elif missing == "unknown_slot":
        PrivateUpgradeStore(stage.root / "99")
    elif missing == "linked_stage":
        (job.root / "source").symlink_to(stage.root)
    elif missing == "torn_claim":
        read = PrivateUpgradeStore.read
        calls = []

        def changing_read(self, key, model):
            value = read(self, key, model)
            if self.root == bootstrap.root and key == "claim.json":
                calls.append(key)
                if len(calls) > 1:
                    return value.model_copy(update={"state": "released"})
            return value

        monkeypatch.setattr(PrivateUpgradeStore, "read", changing_read)
    environment = tmp_path / "envs" / native.candidate.environment_id
    verified = []

    def verify(envs, runtimes, runtime):
        verified.append(runtime)
        return environment / "bin/python3"

    monkeypatch.setattr(observer, "verify_prepared_runtime", verify)
    monkeypatch.setattr(
        observer,
        "sys",
        SimpleNamespace(
            prefix=str(environment if missing != "wrong_interpreter" else tmp_path),
            executable=str(environment / "bin/python3"),
        ),
    )
    if missing:
        with pytest.raises(UpgradeUnavailable):
            observer.observe_retained_expired_recovery(
                settings, request, plan.target, native.candidate
            )
        if missing is True:
            assert not (guard.root / "intent.json").exists()
    else:
        value = observer.observe_retained_expired_recovery(
            settings, request, plan.target, native.candidate
        )
        assert value.native == native and value.scopes == (scope,)
        assert verified == [native.candidate]
        assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
