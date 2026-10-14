"""Maintenance unload never turns active or uncertain work into an idle resident."""

from pathlib import Path
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_upgrade_maintenance import command

from coire_core.errors import UpgradeConflict
from coire_core.models.engine import EngineState
from coire_core.models.upgrades import UpgradeInventoryEntry, UpgradeOwnerInventory
from coire_core.settings import Settings
from coire_node.engines import EngineManager, _Engine
from coire_node.maintenance import REQUIRED_OWNERS, NodeMaintenance


@pytest.mark.parametrize(
    "blocker", ["none", "requests", "training", "finalizing", "unknown", "foreign", "loading"]
)
async def test_only_complete_idle_owners_allow_normal_resident_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    manager = EngineManager(
        Settings(node_state_dir=str(tmp_path / "node")),
        Mock(),
        "127.0.0.1",
        memory_lock=gate.lock,
        admission_check=gate.check,
    )
    identity = uuid4()
    engine = _Engine(
        engine_id=identity,
        slug="tiny",
        port=8000,
        estimate_bytes=1024,
        pid=123,
        create_time=100.0,
        state=EngineState.STARTING if blocker == "loading" else EngineState.READY,
    )
    manager._engines[str(identity)] = engine
    manager._persist()
    monkeypatch.setattr(
        "coire_node.upgrade_inventory.native_process_entries",
        lambda *_: (
            (
                UpgradeInventoryEntry(
                    owner="engines",
                    kind="native-process",
                    work_id="pid-999",
                    state="active",
                    process_id=999,
                    process_created_at=200.0,
                ),
            )
            if blocker == "foreign"
            else ()
        ),
    )
    for owner in REQUIRED_OWNERS - {"requests", "engines"}:
        gate.register(
            owner,
            lambda owner=owner: UpgradeOwnerInventory(
                owner=owner,
                status="unknown" if blocker == "unknown" and owner == "runs" else "complete",
                entries=(
                    UpgradeInventoryEntry(
                        owner=owner,
                        kind="job",
                        work_id="active-job",
                        state="active",
                    ),
                )
                if (blocker == "training" and owner == "training")
                or (blocker == "finalizing" and owner == "training-lifecycle")
                else (),
            ),
        )
    gate.register("engines", manager.upgrade_inventory)
    stop = Mock()
    monkeypatch.setattr(manager, "stop", stop)
    if blocker == "requests":
        with gate.admission():
            gate.apply(command())
            assert (
                await gate.drain_idle_engines(command().maintenance.job_id, 1, "a" * 64, manager)
                == ()
            )
    else:
        gate.apply(command())
        result = await gate.drain_idle_engines(command().maintenance.job_id, 1, "a" * 64, manager)
        assert result == ((identity,) if blocker == "none" else ())
    if blocker == "none":
        stop.assert_called_once_with(identity)
    else:
        stop.assert_not_called()


async def test_drain_refuses_released_epoch_after_async_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coire_core.models.upgrades import NodeMaintenanceCommand

    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    manager = EngineManager(
        Settings(node_state_dir=str(tmp_path / "node")),
        Mock(),
        "127.0.0.1",
        memory_lock=gate.lock,
        admission_check=gate.check,
    )
    gate.apply(command())
    observed = gate.inventory(command().maintenance.job_id, 1, "a" * 64)

    async def released(*_: object) -> object:
        gate.apply(
            NodeMaintenanceCommand(
                maintenance=command().maintenance,
                action="release",
                terminal_verified=True,
            )
        )
        return observed

    monkeypatch.setattr(gate, "inventory_async", released)
    stop = Mock()
    monkeypatch.setattr(manager, "stop", stop)
    with pytest.raises(UpgradeConflict):
        await gate.drain_idle_engines(command().maintenance.job_id, 1, "a" * 64, manager)
    stop.assert_not_called()


@pytest.mark.parametrize("blocker", ["none", "image-job", "unknown-image", "loading-engine"])
async def test_image_and_engine_residents_drain_only_after_joint_idle_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    from coire_node.image_runtime.supervisor import ImageProcessSupervisor, ImageProcessUnavailable

    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    engines = EngineManager(
        Settings(node_state_dir=str(tmp_path / "node")),
        Mock(),
        "127.0.0.1",
        memory_lock=gate.lock,
        admission_check=gate.check,
    )
    monkeypatch.setattr(
        engines, "_idle_resident_identities", lambda: None if blocker == "loading-engine" else ()
    )
    images = Mock()
    images._lock = gate.lock
    identity = uuid4()
    images._idle_resident_identity.return_value = identity
    if blocker == "unknown-image":
        images._idle_resident_identity.side_effect = ImageProcessUnavailable()
    for owner in REQUIRED_OWNERS - {"requests"}:
        gate.register(
            owner,
            lambda owner=owner: UpgradeOwnerInventory(
                owner=owner,
                status="complete",
                entries=(
                    UpgradeInventoryEntry(
                        owner=owner,
                        kind="image-job" if blocker == "image-job" else "image-worker",
                        work_id=str(identity),
                        state="active",
                    ),
                )
                if owner == "images"
                else (),
            ),
        )
    gate.apply(command())
    await gate.drain_idle_engines(
        command().maintenance.job_id,
        1,
        "a" * 64,
        engines,
        cast(ImageProcessSupervisor, images),
    )
    if blocker == "none":
        images.stop.assert_called_once()
        assert images.stop.call_args.args[0].instance_id == identity
    else:
        images.stop.assert_not_called()
