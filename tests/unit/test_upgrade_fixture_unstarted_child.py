"""Retired unpublished child provenance preserves both process histories."""

from datetime import UTC, datetime, timedelta

import pytest
from test_upgrade_native_failure_replay import failure

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeGuardStatus,
    NativeNodeProcess,
    NativeRuntimeSelection,
    NativeUpgradeClaim,
    NodeAgentProcessIdentity,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node import expired_recovery_cold_owner as owner
from coire_node import upgrade_fixture_unstarted_child as capture


@pytest.mark.parametrize(
    "fault",
    [None, "live", "published", "source", "target", "pointer", "definition", "child_changed"],
)
def test_retired_unpublished_candidate_requires_exact_private_provenance(
    tmp_path, monkeypatch, fault
):
    import json

    plan, body = failure()
    native = body.native
    now = datetime.now(UTC)
    settings = Settings(
        upgrade_bootstrap_root=str(tmp_path / "upgrade-bootstrap"),
        node_state_dir=str(tmp_path / "state"),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    bootstrap = PrivateUpgradeStore(tmp_path / "upgrade-bootstrap")
    records = PrivateUpgradeStore(tmp_path / "fixture-records")
    state = PrivateUpgradeStore(tmp_path / "state/upgrades")
    op = PrivateUpgradeStore(bootstrap.root / "operations" / native.job_id)
    guard = PrivateUpgradeStore(op.root / "guard")
    guard.write("intent.json", native)
    claim = NativeUpgradeClaim(
        job_id=native.job_id,
        generation=native.generation,
        plan_sha256=native.plan_sha256,
        node="coire-edge-a",
        updated_at=now,
    )
    bootstrap.write("claim.json", claim)
    original = NativeNodeProcess(
        selected=native.source, process=native.source_process, recorded_at=now
    )
    state.write("node-process.json", original)
    guard_process = NodeAgentProcessIdentity(
        pid=9093,
        create_time_s=(now - timedelta(seconds=2)).timestamp(),
        environment_sha256=native.source.runtime.environment_sha256,
    )
    status = NativeGuardStatus(
        node="coire-edge-a",
        bootstrap_runtime=native.source.runtime,
        process=guard_process,
        selected=native.source,
        ready=False,
        observed_at=now,
    )
    bootstrap.write("status.json", status)
    pointer = NativeRuntimeSelection(
        node="coire-edge-a",
        runtime=native.candidate,
        release_sha256=plan.target.manifest_sha256,
        job_id=native.job_id,
        generation=native.generation,
        selected_at=now,
    )
    PrivateUpgradeStore(op.root / "pointer").write("selection.json", pointer)
    target = tmp_path / "envs" / native.candidate.environment_id
    target.mkdir(parents=True, mode=0o700)
    (tmp_path / "envs/current").symlink_to(target)
    records.write("source.json", plan.source)
    records.write("target.json", plan.target)
    for name, a in [
        (
            "fixture-supervisor.json",
            {"pid": 9090, "create_time": (now - timedelta(seconds=5)).timestamp()},
        ),
        (
            "fixture-processes.json",
            {
                "guard": {"pid": guard_process.pid, "create_time": guard_process.create_time_s},
                "node": {"pid": 9091, "create_time": (now - timedelta(seconds=1)).timestamp()},
            },
        ),
    ]:
        p = records.path(name)
        p.write_text(json.dumps(a))
        p.chmod(0o600)
    definition = tmp_path / "fixture-supervisor.py"
    definition.write_text("unchanged controlled fixture definition")
    definition.chmod(0o600)
    retired = []

    def verify(identity):
        if fault == "live" and identity.pid == 9091:
            raise UpgradeUnavailable("candidate remains live")
        retired.append(identity.pid)

    monkeypatch.setattr(capture, "verify_retired_identity", verify)
    monkeypatch.setattr(owner, "verify_retired_identity", verify)
    monkeypatch.setattr(capture, "verify_prepared_runtime", lambda *_: target / "bin/python3")
    if fault == "published":
        state.write("node-process.json", original.model_copy(update={"process": guard_process}))
    if fault == "source":
        records.write("source.json", plan.target)
    if fault == "target":
        records.write("target.json", plan.source)
    if fault == "pointer":
        PrivateUpgradeStore(op.root / "pointer", create=False).write(
            "selection.json", pointer.model_copy(update={"generation": 2})
        )
    if fault in {"live", "published", "source", "target", "pointer"}:
        with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
            capture.capture_retired_unstarted_child(settings, native.job_id)
        assert not records.path("retired-unstarted-" + native.job_id + ".json").exists()
        return
    proof = capture.capture_retired_unstarted_child(settings, native.job_id)
    assert capture.capture_retired_unstarted_child(settings, native.job_id) == proof
    if fault == "definition":
        definition.write_text("changed definition")
    if fault == "child_changed":
        p = records.path("fixture-processes.json")
        a = json.loads(p.read_text())
        a["node"]["pid"] = 9999
        p.write_text(json.dumps(a))
    if fault:
        with pytest.raises(UpgradeConflict):
            owner.observe_fixture_supervisor(settings, status, original)
    else:
        observed = owner.observe_fixture_supervisor(settings, status, original)
        assert observed.pid == 9090 and {9090, 9091, native.source_process.pid, 9093} <= set(
            retired
        )
