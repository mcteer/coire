"""Strict inventories preserve uncertainty and omit process command secrets."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import psutil
import pytest

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.settings import Settings
from coire_node.runs import RunManager
from coire_node.upgrade_inventory import native_process_entries, read_owner_json


def test_native_scan_bounds_all_processes_even_without_matching_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=123)
    process.cmdline.return_value = ["unrelated"]
    examined = 0

    def processes():
        nonlocal examined
        for _ in range(10000):
            examined += 1
            yield process

    monkeypatch.setattr(psutil, "process_iter", processes)
    with pytest.raises(UpgradeUnavailable, match="bound"):
        native_process_entries("engines", ("mlx_lm.server",))
    assert examined == 4097


async def test_run_inventory_retains_terminal_relays_and_rejects_uncertain_identity() -> None:
    from uuid import uuid4

    docker = Mock()
    row = {
        "Id": "a" * 64,
        "State": "exited",
        "Labels": {
            "com.coire.managed": "true",
            "com.coire.node": "coire-edge-a",
            "com.coire.agent-run": str(uuid4()),
            "com.coire.component": "run-relay",
        },
    }
    docker.list_containers = AsyncMock(return_value=[row])
    manager = RunManager(Settings(node_name="coire-edge-a"), docker)
    inventory = await manager.upgrade_inventory()
    assert inventory.status == "complete" and inventory.entries[0].state == "cleaning"
    docker.list_containers.return_value = []
    assert (await manager.upgrade_inventory()).entries == ()
    docker.list_containers.return_value = [row, row]
    with pytest.raises(UpgradeUnavailable):
        await manager.upgrade_inventory()
    docker.list_containers.return_value = [{**row, "State": "unknown"}]
    with pytest.raises(UpgradeUnavailable):
        await manager.upgrade_inventory()
    docker.list_containers.side_effect = ConnectionError("docker unavailable")
    with pytest.raises(ConnectionError):
        await manager.upgrade_inventory()


def test_native_identity_and_evidence_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(pid=123)
    process.cmdline.return_value = ["python", "-m", "mlx_lm.server", "--token", "secret-value"]
    process.create_time.return_value = 100.0
    monkeypatch.setattr(psutil, "process_iter", lambda: [process])
    monkeypatch.setattr(psutil, "Process", lambda _: process)
    entries = native_process_entries("engines", ("mlx_lm.server",))
    assert len(entries) == 1 and entries[0].process_id == 123
    assert entries[0].process_created_at == 100.0
    assert "secret-value" not in entries[0].model_dump_json()
    changed = Mock()
    changed.create_time.return_value = 200.0
    monkeypatch.setattr(psutil, "Process", lambda _: changed)
    with pytest.raises(UpgradeUnavailable):
        native_process_entries("engines", ("mlx_lm.server",))


def test_unreadable_script_host_is_not_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(pid=123)
    process.cmdline.side_effect = psutil.AccessDenied(123)
    monkeypatch.setattr(psutil, "process_iter", lambda: [process])
    monkeypatch.setattr(
        "coire_node.upgrade_inventory.verified_native_system_process", lambda _: False
    )
    with pytest.raises(UpgradeUnavailable):
        native_process_entries("engines", ("mlx_lm.server",))


def test_owner_document_rejects_duplicate_fields_and_link(tmp_path: Path) -> None:
    path = tmp_path / "owner.json"
    path.write_bytes(b'{"pid":1,"pid":2}')
    path.chmod(0o600)
    with pytest.raises(UpgradeUnavailable):
        read_owner_json(path)
    path.write_bytes(b"[]")
    assert read_owner_json(path) == []
    path.rename(tmp_path / "outside.json")
    path.symlink_to(tmp_path / "outside.json")
    with pytest.raises(OSError):
        read_owner_json(path)


async def test_core_job_inventory_retains_callback_cleanup_and_bounds() -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from coire_api.upgrades.inventory import job_inventory

    session = AsyncMock(spec=AsyncSession)
    result = Mock()
    result.all.return_value = []
    session.scalars.return_value = result
    owners = await job_inventory(session)
    assert {owner.owner for owner in owners} == {"scheduler", "runs", "mcp", "ops"}
    queries = [str(call.args[0]) for call in session.scalars.call_args_list]
    assert any("workspace_cleaned_at IS NULL" in query for query in queries)
    assert any("run_commands" in query and "EXISTS" in query for query in queries)
    assert all(owner.status == "complete" and not owner.entries for owner in owners)
    result.all.return_value = ["job-" + str(index) for index in range(4097)]
    with pytest.raises(UpgradeUnavailable):
        await job_inventory(session)


async def test_service_inventory_rejects_foreign_owner_and_transport_failure() -> None:
    import httpx
    from pydantic import SecretStr

    from coire_api.upgrades.inventory import service_inventory

    bodies = [{"owner": "ops", "status": "complete", "entries": []}]

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/upgrades/inventory"
        assert request.headers["authorization"] == "Bearer scoped-executor"
        return httpx.Response(200, json=bodies[0])

    settings = Settings(upgrade_executor_token=SecretStr("scoped-executor"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(UpgradeUnavailable):
            await service_inventory(settings, "coire-mcp", client=client)
        bodies[0] = {"owner": "mcp", "status": "complete", "entries": []}
        assert not (await service_inventory(settings, "coire-mcp", client=client)).entries
        bodies[0] = {"owner": "mcp", "status": "complete", "entries": [], "environment": "secret"}
        with pytest.raises(UpgradeUnavailable):
            await service_inventory(settings, "coire-mcp", client=client)

    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private transport details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(UpgradeUnavailable) as error:
            await service_inventory(settings, "coire-mcp", client=client)
        assert "private transport details" not in str(error.value)


@pytest.mark.parametrize(
    ("service", "owner", "host", "token"),
    (
        ("coire-mcp", "mcp", "coire-mcp", "executor-token"),
        ("coire-ops", "ops", "coire-ops", "ops-token"),
        ("coire-file-worker", "files", "coire-file-worker", "files-token"),
    ),
)
async def test_independent_inventory_uses_only_its_service_authority(
    service: str, owner: str, host: str, token: str
) -> None:
    import httpx
    from pydantic import SecretStr

    from coire_api.upgrades.inventory import service_inventory

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == host
        assert request.headers["authorization"] == "Bearer " + token
        return httpx.Response(200, json={"owner": owner, "status": "complete", "entries": []})

    settings = Settings(
        upgrade_executor_token=SecretStr("executor-token"),
        ops_service_token=SecretStr("ops-token"),
        file_worker_service_token=SecretStr("files-token"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert (await service_inventory(settings, service, client=client)).owner == owner


def test_core_failover_inventory_preserves_election_and_request_obligations() -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from pydantic import SecretStr

    from coire_api.upgrades.inventory import failover_inventory
    from coire_core.failover_election import ServiceRole

    settings = Settings(
        failover_member_name="coire-core", failover_peer_key=SecretStr("configured")
    )
    with patch("coire_api.routes.failover.get_participant", return_value=None):
        assert failover_inventory(settings).status == "unknown"
    participant = SimpleNamespace(
        role=ServiceRole.ELECTED, in_flight=0, frontend_ready=lambda _: True
    )
    with patch("coire_api.routes.failover.get_participant", return_value=participant):
        inventory = failover_inventory(settings)
        assert inventory.status == "complete" and inventory.entries[0].state == "standing"
        participant.in_flight = 1
        assert failover_inventory(settings).entries[0].state == "active"
        participant.in_flight = 0
        participant.role = ServiceRole.DRAINING
        assert failover_inventory(settings).entries[0].state == "active"
        participant.role = ServiceRole.ELECTED
        participant.frontend_ready = lambda _: False
        assert failover_inventory(settings).status == "unknown"


async def test_core_aggregate_uses_scope_and_rechecks_authority_after_external_reads() -> None:
    from unittest.mock import patch

    from sqlalchemy.ext.asyncio import AsyncSession
    from upgrade_fixtures import core_plan

    from coire_api.upgrades.admission import CoreAdmissions
    from coire_api.upgrades.inventory import core_inventory, core_required_owners
    from coire_core.models.upgrades import UpgradeOwnerInventory

    original = core_plan()
    value = original.model_copy(
        update={
            "operation": original.operation.model_copy(update={"cohort": ("coire-file-worker",)}),
            "maintenance": original.maintenance.model_copy(
                update={"targets": ("coire-file-worker",)}
            ),
        }
    )
    assert core_required_owners(value) == frozenset({"requests", "files"})
    session = AsyncMock(spec=AsyncSession)
    reads: list[str] = []

    async def authority(*_: object) -> None:
        reads.append("authority")

    async def remote(*_: object, **__: object) -> UpgradeOwnerInventory:
        reads.append("remote")
        return UpgradeOwnerInventory(owner="files", status="complete")

    with (
        patch("coire_api.upgrades.inventory.require_inventory_authority", side_effect=authority),
        patch(
            "coire_api.upgrades.inventory.privacy_inventory",
            AsyncMock(return_value=(UpgradeOwnerInventory(owner="files", status="complete"),)),
        ),
        patch("coire_api.upgrades.inventory.service_inventory", side_effect=remote),
        patch("coire_api.upgrades.inventory.job_inventory", AsyncMock()) as jobs,
    ):
        result = await core_inventory(
            session, Settings(upgrades_enabled=True), value, CoreAdmissions(enabled=True)
        )
    assert result.idle(core_required_owners(value))
    assert reads == ["authority", "remote", "authority"]
    jobs.assert_not_called()


async def test_core_aggregate_retains_unknown_service_and_changed_fence() -> None:
    from unittest.mock import patch

    from sqlalchemy.ext.asyncio import AsyncSession
    from upgrade_fixtures import core_plan

    from coire_api.upgrades.admission import CoreAdmissions
    from coire_api.upgrades.inventory import core_inventory, core_required_owners
    from coire_core.errors import UpgradeConflict
    from coire_core.models.upgrades import UpgradeOwnerInventory

    original = core_plan()
    value = original.model_copy(
        update={
            "operation": original.operation.model_copy(update={"cohort": ("coire-file-worker",)}),
            "maintenance": original.maintenance.model_copy(
                update={"targets": ("coire-file-worker",)}
            ),
        }
    )
    settings = Settings(upgrades_enabled=True)
    session = AsyncMock(spec=AsyncSession)
    with (
        patch("coire_api.upgrades.inventory.require_inventory_authority", AsyncMock()) as authority,
        patch(
            "coire_api.upgrades.inventory.privacy_inventory",
            AsyncMock(return_value=(UpgradeOwnerInventory(owner="files", status="complete"),)),
        ),
        patch(
            "coire_api.upgrades.inventory.service_inventory",
            AsyncMock(side_effect=UpgradeUnavailable("private host failure")),
        ),
    ):
        result = await core_inventory(session, settings, value, CoreAdmissions(enabled=True))
        assert not result.idle(core_required_owners(value))
        assert next(owner for owner in result.owners if owner.owner == "files").status == "unknown"
        assert "private host failure" not in result.model_dump_json()
        authority.reset_mock()
        authority.side_effect = [None, UpgradeConflict("Fence changed during collection")]
        with pytest.raises(UpgradeConflict):
            await core_inventory(session, settings, value, CoreAdmissions(enabled=True))


@pytest.mark.parametrize(
    "condition,error,reason",
    [
        ("missing", UpgradeUnavailable, "incomplete"),
        ("partial", UpgradeUnavailable, "incomplete"),
        ("older_released", UpgradeUnavailable, "incomplete"),
        ("foreign_active", UpgradeConflict, "Another operation"),
        ("newer_released", UpgradeConflict, "stale"),
        ("matched_released", UpgradeConflict, "already released"),
        ("payload_changed", UpgradeConflict, "changed"),
        ("complete", None, None),
    ],
)
async def test_pending_core_barriers_never_authorize_foreign_or_changed_ownership(
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
    error: type[Exception] | None,
    reason: str | None,
) -> None:
    from types import SimpleNamespace

    from upgrade_fixtures import core_plan

    from coire_api.upgrades.inventory import require_inventory_authority
    from coire_core.models.upgrades import UpgradeExecutionPlan

    original = core_plan()
    plan = UpgradeExecutionPlan.model_validate(
        original.model_dump()
        | {
            "operation": original.operation.model_dump() | {"generation": 3},
            "maintenance": original.maintenance.model_dump() | {"generation": 3},
        }
    )
    row = SimpleNamespace(
        payload=plan.operation.model_dump(mode="json"), execution_plan=plan.model_dump(mode="json")
    )
    monkeypatch.setattr("coire_api.upgrades.persistence.get_operation", AsyncMock(return_value=row))
    barriers = [
        SimpleNamespace(
            job_id=plan.operation.job_id,
            generation=3,
            released=False,
            payload=plan.maintenance.model_dump(mode="json"),
        )
        for _ in plan.maintenance.targets
    ]
    if condition == "missing":
        barriers = []
    elif condition == "partial":
        barriers.pop()
    elif condition in {"older_released", "foreign_active"}:
        barriers[0].job_id = "01J00000000000000000000001"
        barriers[0].generation = 2
        barriers[0].released = condition == "older_released"
    elif condition == "newer_released":
        barriers[0].generation = 4
        barriers[0].released = True
    elif condition == "matched_released":
        barriers[0].released = True
    elif condition == "payload_changed":
        barriers[0].payload["generation"] = 4
    session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: barriers)))
    if error:
        with pytest.raises(error, match=reason):
            await require_inventory_authority(session, plan)
    else:
        await require_inventory_authority(session, plan)
