"""Core coordination keeps physical ownership ahead of admission release."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import UpgradePhase
from coire_core.settings import Settings
from coire_scheduler import upgrades


@pytest.mark.parametrize("previous", ["absent", "released_older", "active_older", "released_newer"])
async def test_core_establishes_and_reconciles_peer_fences_without_effect_commands(
    monkeypatch, previous
):
    plan = core_plan()
    plan = plan.model_copy(
        update={
            "operation": plan.operation.model_copy(update={"generation": 2}),
            "maintenance": plan.maintenance.model_copy(update={"generation": 2}),
        }
    )
    row = SimpleNamespace(
        payload=plan.operation.model_dump(mode="json"),
        execution_plan=plan.model_dump(mode="json"),
    )
    session = AsyncMock()
    session.get.return_value = None
    if previous != "absent":
        old = plan.maintenance.model_copy(
            update={
                "job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "generation": plan.operation.generation
                + (1 if previous == "released_newer" else -1),
            }
        )
        session.get.return_value = SimpleNamespace(
            payload=old.model_dump(mode="json"), released=previous != "active_older"
        )

    @asynccontextmanager
    async def scope():
        yield session

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.apply_upgrade_maintenance.return_value = SimpleNamespace(
        released=False, generation=plan.operation.generation
    )
    monkeypatch.setattr(upgrades, "session_scope", scope)
    monkeypatch.setattr(upgrades, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(upgrades, "NodeClient", lambda *args, **kwargs: client)
    fence = AsyncMock()
    monkeypatch.setattr(upgrades, "fence", fence)
    if previous in {"active_older", "released_newer"}:
        with pytest.raises(UpgradeConflict):
            await upgrades.reconcile_core_operation(
                plan.operation.job_id, Settings(upgrades_enabled=True)
            )
        fence.assert_not_awaited()
        client.apply_upgrade_maintenance.assert_not_awaited()
        return
    assert not await upgrades.reconcile_core_operation(
        plan.operation.job_id, Settings(upgrades_enabled=True)
    )
    assert fence.await_count == 2
    active = fence.await_args.args[1]
    assert active.state == "active"
    assert active.acknowledged_generations == {
        "coire-edge-a": plan.operation.generation,
        "coire-edge-b": plan.operation.generation,
    }
    assert [call.args[1].action for call in client.apply_upgrade_maintenance.await_args_list] == [
        "apply",
        "apply",
        "reconcile",
        "reconcile",
    ]
    client.execute_upgrade.assert_not_awaited()
    client.cancel_upgrade.assert_not_awaited()


async def test_terminal_database_status_cannot_release_without_physical_selection(
    monkeypatch, tmp_path: Path
):
    plan = core_plan()
    operation = plan.operation.model_copy(
        update={"phase": UpgradePhase.SUCCEEDED, "finished_at": datetime.now(UTC)}
    )
    row = SimpleNamespace(
        payload=operation.model_dump(mode="json"),
        execution_plan=plan.model_dump(mode="json"),
    )

    @asynccontextmanager
    async def scope():
        yield AsyncMock()

    client = AsyncMock()
    client.__aenter__.return_value = client
    release = AsyncMock()
    monkeypatch.setattr(upgrades, "session_scope", scope)
    monkeypatch.setattr(upgrades, "get_operation", AsyncMock(return_value=row))
    monkeypatch.setattr(upgrades, "NodeClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(upgrades, "release_verified", release)
    with pytest.raises(UpgradeConflict):
        await upgrades.reconcile_core_operation(
            plan.operation.job_id,
            Settings(
                upgrades_enabled=True, upgrade_coordination_dir=str(tmp_path / "coordination")
            ),
        )
    release.assert_not_awaited()
    client.apply_upgrade_maintenance.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["closed", "partial", "foreign", "lost_ack", "unauthorized"])
async def test_core_peer_closure_preserves_epoch_and_never_drains_existing_work(
    outcome: str,
) -> None:
    from coire_api.nodes_client import NodeError, NodeErrorKind

    original = core_plan()
    plan = original.model_copy(
        update={
            "operation": original.operation.model_copy(
                update={"phase": UpgradePhase.FAILED_UNCHANGED, "finished_at": datetime.now(UTC)}
            )
        }
    )
    client = AsyncMock()
    conflict = NodeError(NodeErrorKind.CONFLICT, "coire-edge-a")
    if outcome == "partial":
        client.apply_upgrade_maintenance.side_effect = [conflict, None, None]
    elif outcome == "foreign":
        client.apply_upgrade_maintenance.side_effect = [conflict, conflict]
    elif outcome in {"lost_ack", "unauthorized"}:
        kind = NodeErrorKind.UNREACHABLE if outcome == "lost_ack" else NodeErrorKind.UNAUTHORIZED
        client.apply_upgrade_maintenance.side_effect = NodeError(kind, "coire-edge-a")
    if outcome in {"foreign", "lost_ack", "unauthorized"}:
        with pytest.raises(NodeError):
            await upgrades.release_core_peer_maintenance(client, "coire-edge-a", plan)
    else:
        await upgrades.release_core_peer_maintenance(client, "coire-edge-a", plan)
    commands = [call.args[1] for call in client.apply_upgrade_maintenance.await_args_list]
    assert [command.action for command in commands] == (
        ["release", "apply", "release"]
        if outcome == "partial"
        else ["release", "apply"]
        if outcome == "foreign"
        else ["release"]
    )
    assert all(
        command.maintenance.generation == original.maintenance.generation for command in commands
    )
    assert all(
        command.maintenance.drain_deadline_at == original.maintenance.drain_deadline_at
        for command in commands
    )
    assert all(command.maintenance.job_id == original.maintenance.job_id for command in commands)
    if len(commands) > 1:
        assert commands[1].maintenance.state == "recovering"
    client.execute_upgrade.assert_not_awaited()
    client.cancel_upgrade.assert_not_awaited()


async def test_nonterminal_core_peer_closure_is_rejected_before_any_rpc() -> None:
    client = AsyncMock()
    with pytest.raises(UpgradeConflict, match="terminal operation scope"):
        await upgrades.release_core_peer_maintenance(client, "coire-edge-a", core_plan())
    client.apply_upgrade_maintenance.assert_not_awaited()
