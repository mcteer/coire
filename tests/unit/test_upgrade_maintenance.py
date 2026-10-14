"""Race, crash and missing-owner proofs for the durable admission barrier."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.upgrades import (
    NodeMaintenanceCommand,
    UpgradeMaintenance,
    UpgradeOwnerInventory,
)
from coire_node.maintenance import REQUIRED_OWNERS, NodeMaintenance

NOW = datetime.now(UTC)


def command(generation: int = 1) -> NodeMaintenanceCommand:
    return NodeMaintenanceCommand(
        maintenance=UpgradeMaintenance(
            job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            generation=generation,
            targets=("coire-edge-a",),
            state="draining",
            updated_at=NOW,
            drain_deadline_at=NOW + timedelta(seconds=1),
        )
    )


@pytest.mark.parametrize("field", ["targets", "drain_deadline_at", "initiator_user_id"])
def test_epoch_authority_cannot_change_on_replay(tmp_path: Path, field: str) -> None:
    from uuid import uuid4

    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    original = command()
    gate.apply(original)
    updates: dict[str, object] = {
        "targets": ("coire-edge-a", "coire-edge-b"),
        "drain_deadline_at": NOW + timedelta(seconds=300),
        "initiator_user_id": uuid4(),
    }
    changed = original.model_copy(
        update={"maintenance": original.maintenance.model_copy(update={field: updates[field]})}
    )
    with pytest.raises(UpgradeConflict, match="authority"):
        gate.apply(changed)
    assert gate.current() == original.maintenance


def test_release_replay_observes_closure_with_new_requests(tmp_path: Path) -> None:
    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    original = command()
    gate.apply(original)
    release = original.model_copy(update={"action": "release", "terminal_verified": True})
    gate.apply(release)
    closed = gate.current()
    with gate.admission():
        assert gate.apply(release).released
    assert gate.current() == closed


def test_fence_survives_restart_and_expired_drain(tmp_path: Path) -> None:
    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    value = command(2)
    gate.apply(value)
    restored = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    with pytest.raises(UpgradeUnavailable), restored.admission():
        pass
    with pytest.raises(UpgradeConflict):
        restored.apply(command(1))


def test_admitted_request_remains_a_drain_blocker(tmp_path: Path) -> None:
    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    with gate.admission():
        gate.apply(command())
        assert not gate.inventory(command().maintenance.job_id, 1, "a" * 64).idle(REQUIRED_OWNERS)
        with pytest.raises(UpgradeConflict):
            gate.apply(
                NodeMaintenanceCommand(
                    maintenance=command().maintenance, action="release", terminal_verified=True
                )
            )
    gate.apply(
        NodeMaintenanceCommand(
            maintenance=command().maintenance, action="release", terminal_verified=True
        )
    )
    with gate.admission():
        pass
    with pytest.raises(UpgradeConflict):
        gate.apply(command())


@pytest.mark.parametrize("failed_owner", sorted(REQUIRED_OWNERS - {"requests"}))
def test_unknown_owner_is_never_idle(tmp_path: Path, failed_owner: str) -> None:
    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    gate.apply(command())

    failing: set[str] = set()

    def collector(owner: str) -> Callable[[], UpgradeOwnerInventory]:
        def collect() -> UpgradeOwnerInventory:
            if owner in failing:
                raise OSError("unreadable owner")
            return UpgradeOwnerInventory(owner=owner, status="complete")

        return collect

    for owner in REQUIRED_OWNERS - {"requests"}:
        gate.register(owner, collector(owner))
    assert gate.inventory(command().maintenance.job_id, 1, "a" * 64).idle(REQUIRED_OWNERS)

    failing.add(failed_owner)
    inventory = gate.inventory(command().maintenance.job_id, 1, "a" * 64)
    assert not inventory.idle(REQUIRED_OWNERS)
    assert next(item for item in inventory.owners if item.owner == failed_owner).status == "unknown"


def test_corrupt_or_linked_state_cannot_reopen_admission(tmp_path: Path) -> None:
    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    gate.apply(command())
    path = gate.store.path("maintenance.json")
    path.write_text("broken")
    with pytest.raises(UpgradeUnavailable):
        gate.blocked()
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(UpgradeUnavailable):
        gate.blocked()


async def test_async_owner_failure_and_authority_change_are_never_idle(tmp_path: Path) -> None:
    gate = NodeMaintenance(tmp_path / "async-state", "coire-edge-a")
    gate.apply(command())

    release_during_collection = False

    async def collect() -> UpgradeOwnerInventory:
        if not release_during_collection:
            raise ConnectionError("owner unavailable")
        gate.apply(
            NodeMaintenanceCommand(
                maintenance=command().maintenance, action="release", terminal_verified=True
            )
        )
        return UpgradeOwnerInventory(owner="runs", status="complete")

    gate.register_async("runs", collect)
    inventory = await gate.inventory_async(command().maintenance.job_id, 1, "a" * 64)
    assert next(item for item in inventory.owners if item.owner == "runs").status == "unknown"
    release_during_collection = True
    with pytest.raises(UpgradeConflict):
        await gate.inventory_async(command().maintenance.job_id, 1, "a" * 64)


def test_engine_owner_checks_fence_before_spawn(tmp_path: Path) -> None:
    from unittest.mock import Mock, patch
    from uuid import uuid4

    from coire_core.settings import Settings
    from coire_node.engines import EngineManager

    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    store = Mock()
    store.read_manifest.return_value = None
    engines = EngineManager(
        Settings(node_state_dir=str(tmp_path / "node")),
        store,
        "127.0.0.1",
        memory_lock=gate.lock,
        admission_check=gate.check,
    )
    gate.apply(command())
    with patch("coire_node.engines.subprocess.Popen") as spawn:
        with pytest.raises(UpgradeUnavailable):
            engines.start(engine_id=uuid4(), slug="owner--tiny", estimate_bytes=1024)
        spawn.assert_not_called()


async def test_stream_permit_drains_without_cancellation(tmp_path: Path) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    import httpx
    from fastapi import Depends, FastAPI
    from fastapi.responses import StreamingResponse

    from coire_node.maintenance import require_admission

    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    app = FastAPI()
    app.state.maintenance = gate
    started, finish = asyncio.Event(), asyncio.Event()

    @app.post("/stream", dependencies=[Depends(require_admission)])
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            started.set()
            yield b"before"
            await finish.wait()
            yield b"after"

        return StreamingResponse(body())

    @app.post("/cleanup")
    async def cleanup() -> dict[str, bool]:
        return {"cleaned": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://node"
    ) as client:
        admitted = asyncio.create_task(client.post("/stream"))
        await asyncio.wait_for(started.wait(), timeout=2)
        gate.apply(command())
        denied = await client.post("/stream")
        assert denied.status_code == 503
        assert denied.headers["retry-after"] == "5"
        assert (await client.post("/cleanup")).status_code == 200
        inventory = gate.inventory(command().maintenance.job_id, 1, "a" * 64)
        assert next(owner for owner in inventory.owners if owner.owner == "requests").entries
        finish.set()
        result = await asyncio.wait_for(admitted, timeout=2)
        assert result.content == b"beforeafter"
        inventory = gate.inventory(command().maintenance.job_id, 1, "a" * 64)
        assert not next(owner for owner in inventory.owners if owner.owner == "requests").entries


def test_acquisition_cannot_spawn_or_resume_under_fence(tmp_path: Path) -> None:
    from unittest.mock import Mock, patch
    from uuid import uuid4

    from coire_core.models.jobs import JobKind
    from coire_core.settings import Settings
    from coire_node.jobs import JobSupervisor

    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    jobs = JobSupervisor(
        Settings(node_state_dir=str(tmp_path / "node")),
        Mock(),
        memory_lock=gate.lock,
        admission_check=gate.check,
    )
    gate.apply(command())
    with patch("coire_node.jobs.subprocess.Popen") as spawn:
        with pytest.raises(UpgradeUnavailable):
            jobs.start(job_id=uuid4(), kind=JobKind.PULL, slug="owner--tiny", params={})
        with pytest.raises(UpgradeUnavailable):
            jobs._spawn(uuid4())
        spawn.assert_not_called()
    assert not list((tmp_path / "node/jobs").iterdir())


async def test_training_prepare_cannot_take_holds_after_fence(tmp_path: Path) -> None:
    from training_measurement_fixtures import experiment

    from coire_node.training.journal import TrainingJournal
    from coire_node.training.supervisor import TrainingSupervisor

    prepared = experiment()[1].commands[0].prepare
    assert prepared.node == "coire-edge-a"
    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    journal = TrainingJournal(tmp_path / "attempts", node=prepared.node, admission_lock=gate.lock)
    runtime = TrainingSupervisor(
        journal,
        interpreter=Path("/opt/coire/envs/test/bin/python3"),
        validate_ready=lambda _: None,
        admission_check=gate.check,
    )
    gate.apply(command())
    try:
        with pytest.raises(UpgradeUnavailable):
            await runtime.prepare(
                prepared, memory_available=100 * 1024**3, disk_available=100 * 1024**3
            )
        assert journal.records() == []
        assert journal.held_bytes() == (0, 0)
    finally:
        journal.close()


def test_sync_inventory_rejects_fence_released_during_collection(tmp_path: Path) -> None:
    gate = NodeMaintenance(tmp_path / "sync-authority", "coire-edge-a")
    gate.apply(command())

    def released_during_collection() -> UpgradeOwnerInventory:
        gate.apply(
            NodeMaintenanceCommand(
                maintenance=command().maintenance,
                action="release",
                terminal_verified=True,
            )
        )
        return UpgradeOwnerInventory(owner="training", status="complete")

    gate.register("training", released_during_collection)
    with pytest.raises(UpgradeConflict, match="authority changed"):
        gate.inventory(command().maintenance.job_id, 1, "a" * 64)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_duplicate_inventory_owner_cannot_replace_original(
    tmp_path: Path, asynchronous: bool
) -> None:
    gate = NodeMaintenance(tmp_path / "registration", "coire-edge-a")

    def collect() -> UpgradeOwnerInventory:
        return UpgradeOwnerInventory(owner="training", status="unknown", reason="owner_unavailable")

    async def collect_async() -> UpgradeOwnerInventory:
        return collect()

    if asynchronous:
        gate.register_async("training", collect_async)
        with pytest.raises(ValueError, match="duplicate"):
            gate.register_async("training", collect_async)
    else:
        gate.register("training", collect)
        with pytest.raises(ValueError, match="duplicate"):
            gate.register("training", collect)


def test_verified_unchanged_abort_preserves_known_requests_and_resumes_admission(
    tmp_path: Path,
) -> None:
    gate = NodeMaintenance(tmp_path / "state", "coire-edge-a")
    original = command()
    with gate.admission():
        gate.apply(original)
        release = NodeMaintenanceCommand(
            maintenance=original.maintenance,
            action="release",
            terminal_verified=True,
            source_unchanged=True,
            source_runtime_sha256="a" * 64,
        )
        with pytest.raises(UpgradeConflict):
            gate.apply(release)
        assert gate.apply(release, allow_existing_work=True).released
        with gate.admission():
            pass
    closed = gate.current()
    assert gate.apply(release, allow_existing_work=True).released
    assert gate.current() == closed


@pytest.mark.parametrize("cleaning_owner", sorted(REQUIRED_OWNERS - {"requests"}))
async def test_terminal_cleanup_for_each_owner_cannot_release_native_fence(
    tmp_path: Path, cleaning_owner: str
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from fastapi import HTTPException

    from coire_core.models.upgrades import UpgradeInventoryEntry
    from coire_node.routes.upgrades import apply_maintenance

    gate = NodeMaintenance(tmp_path / "cleanup-state", "coire-edge-a")
    accepted = command()
    gate.apply(accepted)
    held = UpgradeInventoryEntry(
        owner=cleaning_owner,
        kind="terminal-work-cleanup",
        work_id="retained-work",
        state="cleaning",
        holds=("owned-lease",),
    )
    for owner in sorted(REQUIRED_OWNERS - {"requests"}):
        gate.register(
            owner,
            lambda owner=owner: UpgradeOwnerInventory(
                owner=owner, status="complete", entries=(held,) if owner == cleaning_owner else ()
            ),
        )
    authority = Mock()
    authority.observed.return_value = SimpleNamespace(
        runtime=SimpleNamespace(environment_sha256="a" * 64)
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(maintenance=gate, upgrades=authority))
    )
    release = accepted.model_copy(update={"action": "release", "terminal_verified": True})
    with pytest.raises(HTTPException) as refused:
        await apply_maintenance(accepted.maintenance.job_id, release, request)
    assert refused.value.status_code == 409
    assert gate.current() == accepted.maintenance
    authority.verify_terminal.assert_not_called()
    authority.release_claim.assert_not_called()
    assert not gate.inventory(accepted.maintenance.job_id, 1, "a" * 64).idle(REQUIRED_OWNERS)


@pytest.mark.parametrize(
    "fault", [None, "foreign_preimage", "stale", "released_target", "other_target"]
)
def test_recovery_transfer_preserves_continuous_held_maintenance(tmp_path, fault):
    old = command().maintenance.model_copy(update={"state": "fencing"})
    requested = command(2).maintenance.model_copy(
        update={"job_id": "01ARZ3NDEKTSV4RRFFQ69G5FAW", "state": "active"}
    )
    gate = NodeMaintenance(tmp_path / "upgrades", "coire-edge-a")
    gate.apply(NodeMaintenanceCommand(maintenance=old))
    with pytest.raises(UpgradeConflict, match="retains ownership"):
        gate.apply(NodeMaintenanceCommand(maintenance=requested))
    preimage = old
    if fault == "foreign_preimage":
        preimage = old.model_copy(update={"generation": 3})
    if fault == "stale":
        requested = requested.model_copy(update={"generation": 1})
    if fault == "released_target":
        requested = requested.model_copy(update={"state": "released"})
    if fault == "other_target":
        requested = requested.model_copy(update={"targets": ("coire-edge-a", "coire-edge-b")})
    if fault:
        with pytest.raises(UpgradeConflict):
            gate.apply(NodeMaintenanceCommand(maintenance=requested), recovery_preimage=preimage)
        assert gate.current() == old and gate.blocked()
    else:
        result = gate.apply(
            NodeMaintenanceCommand(maintenance=requested), recovery_preimage=preimage
        )
        assert not result.released and gate.current() == requested and gate.blocked()
        assert not gate.apply(
            NodeMaintenanceCommand(maintenance=requested), recovery_preimage=preimage
        ).released
        with pytest.raises(UpgradeUnavailable):
            gate.check()
