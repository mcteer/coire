"""Native selection effects require durable authority and a fresh positive drain."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from upgrade_native_fixtures import native_plan

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    NativeNodeProcess,
    NodeAgentProcessIdentity,
    NodeUpgradeCommand,
    NodeUpgradeInventory,
    UpgradeActivationGrant,
    UpgradeEvidence,
    UpgradeOwnerInventory,
    UpgradePhase,
    UpgradeReceipt,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "incomplete_inventory",
        "stale_inventory",
        "unconfirmed_stop",
        "changed_source_process",
        "pointer_journal_failure",
        "slow_stop",
        "refreshed_inventory",
        "changed_fence_after_stop",
    ],
)
def test_activation_selects_once_only_after_owned_source_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from coire_node import upgrade_executor, upgrade_guard
    from coire_node.maintenance import REQUIRED_OWNERS

    plan, source, candidate = native_plan(tmp_path)
    process = NodeAgentProcessIdentity(
        pid=123456, create_time_s=123.0, environment_sha256=source.runtime.environment_sha256
    )
    journal = upgrade_guard.NativeGuardJournal(
        tmp_path / "operation", plan, source, candidate, process
    )
    now = datetime.now(UTC)
    smoke = UpgradeReceipt(
        job_id=plan.operation.job_id,
        generation=1,
        sequence=1,
        phase=UpgradePhase.SMOKE_BEFORE,
        effect_id="candidate-smoke",
        occurred_at=now,
        outcome="complete",
        observed_sha256=plan.source.manifest_sha256,
        evidence=UpgradeEvidence(
            check="node",
            runtime_sha256=candidate.environment_sha256,
            model_id=plan.model_id,
            model_variant_id=plan.model_variant_id,
            model_manifest_sha256=plan.model_manifest_sha256,
            generated_tokens=10,
            output_sha256="a" * 64,
            cleanup_verified=True,
        ).model_dump_json(),
    )
    journal.receipts.append(smoke)
    grant = UpgradeActivationGrant(
        job_id=plan.operation.job_id,
        generation=1,
        boundary="runtime",
        grant_id=uuid4(),
        expires_at=now + timedelta(seconds=30),
    )
    intent = smoke.model_copy(
        update={
            "sequence": 2,
            "phase": UpgradePhase.ACTIVATING,
            "effect_id": "activation",
            "outcome": "intent",
            "evidence": "",
        }
    )
    journal.record_activation(
        NodeUpgradeCommand.model_validate(
            plan.model_dump(mode="json")
            | {
                "authorization_expires_at": grant.expires_at,
                "activation_grant": grant,
                "activation_receipt": intent,
            }
        )
    )
    settings = Settings(
        node_name="coire-edge-a",
        node_state_dir=str(tmp_path / "state"),
        upgrade_environment_root=str(tmp_path / "envs"),
    )
    node_state = PrivateUpgradeStore(tmp_path / "state/upgrades")
    node_state.write("maintenance.json", plan.maintenance)
    node_state.write(
        "node-process.json",
        NativeNodeProcess(
            selected=source,
            process=process.model_copy(update={"pid": 123457})
            if fault == "changed_source_process"
            else process,
            recorded_at=now,
        ),
    )
    inventory = NodeUpgradeInventory(
        job_id=plan.operation.job_id,
        generation=1,
        node="coire-edge-a",
        runtime_sha256=source.runtime.environment_sha256,
        observed_at=now
        - timedelta(seconds=10 if fault in {"stale_inventory", "refreshed_inventory"} else 0),
        owners=()
        if fault == "incomplete_inventory"
        else tuple(
            UpgradeOwnerInventory(owner=owner, status="complete", entries=())
            for owner in REQUIRED_OWNERS
        ),
    )
    stopped = []
    clock = now

    def stop(*args: object) -> bool:
        nonlocal clock
        stopped.append(args)
        if fault == "slow_stop":
            clock += timedelta(seconds=6)
        elif fault == "changed_fence_after_stop":
            node_state.write(
                "maintenance.json", plan.maintenance.model_copy(update={"generation": 2})
            )
        return fault != "unconfirmed_stop"

    if fault == "slow_stop":
        from types import SimpleNamespace

        monkeypatch.setattr(upgrade_executor, "datetime", SimpleNamespace(now=lambda _: clock))
    monkeypatch.setattr(upgrade_guard, "stop_owned_node_process", stop)
    monkeypatch.setattr(upgrade_executor.platform, "machine", lambda: "arm64")
    if fault == "pointer_journal_failure":
        original_write = journal.store.write

        def interrupted(key: str, value: object, **kwargs: object) -> None:
            if key == "intent.json" and getattr(value, "phase", None) == "candidate_selected":
                raise UpgradeUnavailable("simulated pointer effect journal failure")
            original_write(key, value, **kwargs)

        monkeypatch.setattr(journal.store, "write", interrupted)
    try:
        if fault in {None, "slow_stop", "refreshed_inventory"}:
            selected = upgrade_executor.activate_native_candidate(
                settings,
                journal,
                inventory,
                refresh_inventory=(
                    lambda: inventory.model_copy(update={"observed_at": datetime.now(UTC)})
                )
                if fault == "refreshed_inventory"
                else None,
            )
            assert selected.phase == "candidate_selected"
            assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/target"
            assert (
                upgrade_executor.activate_native_candidate(settings, journal, inventory) == selected
            )
            assert len(stopped) == 1
        else:
            with pytest.raises(UpgradeUnavailable):
                upgrade_executor.activate_native_candidate(settings, journal, inventory)
            assert journal.current().phase == "activation_intent"
            assert (tmp_path / "envs/current").resolve() == tmp_path / "envs" / (
                "target" if fault == "pointer_journal_failure" else "source"
            )
            assert len(stopped) == (
                1
                if fault
                in {"unconfirmed_stop", "pointer_journal_failure", "changed_fence_after_stop"}
                else 0
            )
            if fault == "pointer_journal_failure":
                journal.begin_restoration()
                monkeypatch.setattr(upgrade_guard, "_owned_node_process", lambda *_: None)
                restored = upgrade_executor.restore_native_source(settings, journal)
                assert restored.phase == "source_selected"
                assert (tmp_path / "envs/current").resolve() == tmp_path / "envs/source"
                assert upgrade_executor.restore_native_source(settings, journal) == restored
    finally:
        for path in tmp_path.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
