"""Guard-only transfer: real private locks/journals, simulated native process facts."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_upgrade_recovery_cold_retained_source import continuation_fixture

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrade_recovery_cold_binding import UpgradeColdPostHandbackBinding
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationGrant,
    UpgradeColdSourceKeeperRetirement,
)
from coire_core.models.upgrades import NodeAgentProcessIdentity
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault", [None, "intent", "receipt", "foreign_claim", "foreign_guard", "revoked"]
)
def test_exact_idle_guard_retirement_preserves_node_and_released_preimages(
    tmp_path, monkeypatch, fault
):
    from coire_node import expired_recovery_source_keeper_retirement as worker

    _, _, _, settings, owner, bootstrap, run, _, _, control = continuation_fixture(
        tmp_path, monkeypatch
    )
    original = control.read_cold_recovery_continuation.return_value
    now = owner.intent.accepted_at
    released = original.binding.original_claim.model_copy(
        update={
            "state": "released",
            "updated_at": original.binding.previous_deadline_at - timedelta(seconds=1),
        }
    )
    fence = owner.intent.original_plan.maintenance.model_copy(
        update={"state": "released", "updated_at": released.updated_at}
    )
    status = original.binding.previous_guard.model_copy(
        update={
            "ready": True,
            "observed_at": now,
            "process": NodeAgentProcessIdentity(
                pid=87654,
                create_time_s=(
                    original.binding.previous_deadline_at - timedelta(seconds=5)
                ).timestamp(),
                environment_sha256=owner.original_native.source.runtime.environment_sha256,
            ),
            "bootstrap_runtime": owner.original_native.source.runtime,
        }
    )
    late = UpgradeColdPostHandbackBinding(
        handback_sha256="a" * 64,
        supervision_sha256="b" * 64,
        closure_intent_sha256="c" * 64,
        released_claim=released,
        released_maintenance=fence,
        source_helper=status,
        retired_supervisor=NodeAgentProcessIdentity(
            pid=87653,
            create_time_s=status.process.create_time_s,
            environment_sha256=owner.runtime.environment_sha256,
        ),
    )
    binding = original.binding.model_copy(
        update={
            "previous_guard": status,
            "observed_at": now,
            "post_handback": late,
            "previous_archive_sha256": "a" * 64,
            "previous_history_sha256": "b" * 64,
            "previous_smoke_sha256": "c" * 64,
            "previous_pair_result_sha256s": ("d" * 64, "e" * 64, "f" * 64),
            "previous_pair_qualification_sha256": "a" * 64,
        }
    )
    grant = UpgradeColdContinuationGrant.model_validate(
        original.model_copy(
            update={
                "binding": binding,
                "issued_at": now,
                "intent": original.intent.model_copy(
                    update={
                        "local_recovery_maintenance": fence.model_copy(
                            update={
                                "job_id": original.intent.recovery_id,
                                "generation": original.intent.request.original_generation + 1,
                                "targets": (original.intent.original_plan.operation.node,),
                                "state": "active",
                                "updated_at": original.intent.accepted_at,
                                "drain_deadline_at": original.intent.deadline_at,
                                "initiator_user_id": original.intent.accepted_by,
                                "acknowledged_generations": {},
                            }
                        )
                    }
                ),
            }
        ).model_dump(mode="json")
    )
    owner = owner.model_copy(update={"intent": grant.intent})
    control.read_cold_recovery_continuation.return_value = grant
    control.verify_support_context.side_effect = lambda value: value
    monkeypatch.setattr(worker, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(worker, "verify_passive_support_identity", lambda *_: owner.process)
    python = tmp_path / "graphs/envs/source/bin/python3"
    monkeypatch.setattr(worker, "verify_prepared_runtime", lambda *_: python)
    node = PrivateUpgradeStore(tmp_path / "state/upgrades")
    bootstrap.write("claim.json", released)
    bootstrap.write("status.json", status)
    node.write("maintenance.json", fence)
    node.write("node-process.json", binding.retained_node)
    preserved = {
        "claim": bootstrap.path("claim.json").read_bytes(),
        "node": node.path("node-process.json").read_bytes(),
        "fence": node.path("maintenance.json").read_bytes(),
    }
    lifetime = bootstrap.ownership("lifetime.lock")
    lifetime.__enter__()
    alive = True
    guard = Mock()
    guard.cmdline.return_value = [str(python), "-I", "-B", "-m", "coire_node.upgrade_guard"]

    def terminate():
        nonlocal alive
        alive = False
        lifetime.__exit__(None, None, None)

    guard.terminate.side_effect = terminate
    monkeypatch.setattr(worker, "_owned_native_process", lambda *_: guard if alive else None)
    node_process = Mock()
    monkeypatch.setattr(worker, "_owned_node_process", lambda *_: node_process)

    def retired(_):
        if alive:
            raise UpgradeUnavailable("guard remains live")

    monkeypatch.setattr(worker, "verify_retired_identity", retired)
    if fault == "foreign_claim":
        bootstrap.write(
            "claim.json", released.model_copy(update={"generation": released.generation + 1})
        )
    if fault == "foreign_guard":
        guard.cmdline.return_value = ["foreign"]
    if fault == "revoked":
        control.verify_support_context.side_effect = UpgradeUnavailable("revoked")
    write = PrivateUpgradeStore.write
    lost = False

    def persist(store, key, value, **kwargs):
        nonlocal lost
        result = write(store, key, value, **kwargs)
        if key == f"source.keeper.retirement.{fault}.json" and not lost:
            lost = True
            raise UpgradeUnavailable("lost retirement response")
        return result

    monkeypatch.setattr(PrivateUpgradeStore, "write", persist)
    try:
        with bootstrap.ownership("recovery-transfer.lock"):
            if fault in {"foreign_claim", "foreign_guard", "revoked"}:
                with pytest.raises((UpgradeConflict, UpgradeUnavailable)):
                    worker.retire_source_keeper(settings, grant, control, owner.runtime)
                assert guard.terminate.call_count == 0
                return
            if fault in {"intent", "receipt"}:
                with pytest.raises(UpgradeUnavailable, match="lost retirement"):
                    worker.retire_source_keeper(settings, grant, control, owner.runtime)
            worker.retire_source_keeper(settings, grant, control, owner.runtime)
            worker.retire_source_keeper(settings, grant, control, owner.runtime)
            receipt = run.read(
                "source.keeper.retirement.receipt.json", UpgradeColdSourceKeeperRetirement
            )
            assert receipt.retired_at is not None
            assert guard.terminate.call_count == 1
            node_process.terminate.assert_not_called()
            assert bootstrap.path("claim.json").read_bytes() == preserved["claim"]
            assert node.path("node-process.json").read_bytes() == preserved["node"]
            assert node.path("maintenance.json").read_bytes() == preserved["fence"]
    finally:
        if alive:
            lifetime.__exit__(None, None, None)
