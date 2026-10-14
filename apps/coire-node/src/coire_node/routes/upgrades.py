"""Core-authenticated fixed upgrade authority and persistent maintenance transport."""

import asyncio
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from coire_core.errors import CoireError, UpgradeConflict, UpgradeUnavailable
from coire_core.models import LinkObservation
from coire_core.models.upgrades import (
    CoreNodeSmokeCleanupReceipt,
    CoreNodeSmokeScope,
    NodeMaintenanceAck,
    NodeMaintenanceCommand,
    NodeUpgradeCancel,
    NodeUpgradeCollectiveCleanup,
    NodeUpgradeCollectiveCleanupReceipt,
    NodeUpgradeCollectiveScope,
    NodeUpgradeCommand,
    NodeUpgradeInventory,
    NodeUpgradeStatus,
    UpgradeId,
)
from coire_node.maintenance import REQUIRED_OWNERS, NodeMaintenance
from coire_node.upgrade_control import NativeUpgradeControl

router = APIRouter(prefix="/node", tags=["upgrades"])


@router.post(
    "/upgrades/{job_id}/collective-cleanup", response_model=NodeUpgradeCollectiveCleanupReceipt
)
async def collective_cleanup(
    job_id: UpgradeId, body: NodeUpgradeCollectiveCleanup, request: Request
) -> NodeUpgradeCollectiveCleanupReceipt:
    from coire_node.collective_cleanup import reconcile_collective_cleanup
    from coire_node.upgrade_guard import NativeGuardTransport

    if job_id != body.scope.plan.operation.job_id:
        raise problem(UpgradeConflict("Collective cleanup route authority differs"))
    gate = maintenance(request)
    settings = request.app.state.settings

    def reconcile() -> NodeUpgradeCollectiveCleanupReceipt:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            transport = NativeGuardTransport(settings, client)
            return reconcile_collective_cleanup(
                settings, body, gate, lambda: transport.cleanup_inventories(body)
            )

    return await invoke(reconcile)


@router.post("/upgrades/{job_id}/collective", response_model=LinkObservation)
async def collective(
    job_id: UpgradeId, body: NodeUpgradeCollectiveScope, request: Request
) -> LinkObservation:
    from coire_node.upgrade_collective import run_upgrade_collective

    if job_id != body.plan.operation.job_id:
        raise problem(UpgradeConflict("Upgrade collective route authority differs"))
    gate = maintenance(request)
    runner = request.app.state.link_probes
    if runner is None:
        raise problem(UpgradeUnavailable("Upgrade collective runner is unavailable"))
    return await invoke(
        lambda: run_upgrade_collective(request.app.state.settings, body, gate, runner)
    )


@router.post(
    "/upgrades/{job_id}/smoke/{engine_id}/cleanup", response_model=CoreNodeSmokeCleanupReceipt
)
async def core_smoke_cleanup(
    job_id: UpgradeId,
    engine_id: UUID,
    body: CoreNodeSmokeScope,
    request: Request,
) -> CoreNodeSmokeCleanupReceipt:
    from coire_node.core_smoke import cleanup_core_smoke

    if job_id != body.plan.operation.job_id or engine_id != body.engine_id:
        raise problem(UpgradeConflict("Core smoke cleanup path differs from retained intent"))
    gate = request.app.state.maintenance
    engines = request.app.state.engines
    if gate is None or engines is None or not request.app.state.settings.upgrades_enabled:
        raise problem(UpgradeUnavailable("Core smoke cleanup ownership is unavailable"))
    return await invoke(lambda: cleanup_core_smoke(request.app.state.settings, body, gate, engines))


def problem(error: CoireError) -> HTTPException:
    return HTTPException(
        error.status,
        error.to_problem().model_dump(mode="json"),
        headers={"Retry-After": "5"} if error.status == 503 else None,
    )


async def invoke[T](action: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(action)
    except CoireError as error:
        raise problem(error) from error
    except (OSError, ValueError):
        raise problem(UpgradeUnavailable("Node upgrade state cannot be verified")) from None


def control(request: Request) -> NativeUpgradeControl:
    value: NativeUpgradeControl = request.app.state.upgrades
    return value


def maintenance(request: Request) -> NodeMaintenance:
    value: NodeMaintenance | None = request.app.state.maintenance
    if value is None:
        raise problem(UpgradeUnavailable("Node maintenance bootstrap is unavailable"))
    return value


@router.get("/upgrades/status", response_model=NodeUpgradeStatus)
async def status(request: Request) -> NodeUpgradeStatus:
    return await invoke(control(request).status)


@router.get("/upgrades/{job_id}", response_model=NodeUpgradeStatus)
async def operation_status(
    job_id: UpgradeId,
    request: Request,
    after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
) -> NodeUpgradeStatus:
    return await invoke(lambda: control(request).status(job_id, after_sequence))


@router.put("/upgrades/{job_id}", response_model=NodeUpgradeStatus, status_code=202)
async def execute(
    job_id: UpgradeId, command: NodeUpgradeCommand, request: Request
) -> NodeUpgradeStatus:
    if job_id != command.operation.job_id:
        raise problem(UpgradeConflict("Node upgrade route authority differs"))
    return await invoke(lambda: control(request).accept(command))


@router.post("/upgrades/{job_id}/cancel", response_model=NodeUpgradeStatus, status_code=202)
async def cancel(
    job_id: UpgradeId,
    command: NodeUpgradeCancel,
    request: Request,
    after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
) -> NodeUpgradeStatus:
    result = await invoke(lambda: control(request).cancel(job_id, command))
    if after_sequence:
        return await invoke(lambda: control(request).status(job_id, after_sequence))
    return result


@router.put("/maintenance/{job_id}", response_model=NodeMaintenanceAck)
async def apply_maintenance(
    job_id: UpgradeId, command: NodeMaintenanceCommand, request: Request
) -> NodeMaintenanceAck:
    if job_id != command.maintenance.job_id:
        raise problem(UpgradeConflict("Node maintenance route authority differs"))
    gate = maintenance(request)
    allow_existing_work = False
    previous = await invoke(gate.current)
    if (
        command.action == "release"
        and previous is not None
        and previous.state == "released"
        and (previous.job_id, previous.generation) == (job_id, command.maintenance.generation)
    ):
        # A lost acknowledgement observes the existing closure, even if new
        # requests have since been admitted. It does not release a newer fence.
        acknowledged = await invoke(lambda: gate.apply(command))
        await invoke(lambda: control(request).release_claim(job_id, previous.generation))
        return acknowledged
    if command.action == "release":
        observed = await invoke(control(request).observed)
        if observed is None:
            raise problem(UpgradeUnavailable("Node release runtime observation is unavailable"))
        if command.source_unchanged:
            await invoke(
                lambda: control(request).verify_unchanged_source(
                    job_id, command.maintenance.generation, command.source_runtime_sha256 or ""
                )
            )
            allow_existing_work = True
        try:
            snapshot = await gate.inventory_async(
                job_id, command.maintenance.generation, observed.runtime.environment_sha256
            )
            if not (
                snapshot.reconciled(REQUIRED_OWNERS)
                if allow_existing_work
                else snapshot.idle(REQUIRED_OWNERS)
            ):
                raise UpgradeConflict("Node release ownership remains unreconciled")
        except CoireError as error:
            raise problem(error) from error
        await invoke(
            lambda: control(request).verify_terminal(job_id, command.maintenance.generation)
        )
    acknowledged = await invoke(
        lambda: gate.apply(command, allow_existing_work=allow_existing_work)
    )
    if command.action == "reconcile":
        observed = await invoke(control(request).observed)
        engines = request.app.state.engines
        images = request.app.state.image_workers
        if observed is None or engines is None:
            raise problem(UpgradeUnavailable("Node resident drain owner is unavailable"))
        try:
            await gate.drain_idle_engines(
                job_id,
                command.maintenance.generation,
                observed.runtime.environment_sha256,
                engines,
                images,
            )
        except CoireError as error:
            raise problem(error) from error
    if command.action == "release":
        await invoke(lambda: control(request).release_claim(job_id, command.maintenance.generation))
    return acknowledged


@router.get("/maintenance/{job_id}/inventory", response_model=NodeUpgradeInventory)
async def inventory(
    job_id: UpgradeId, request: Request, generation: Annotated[int, Query(ge=1, le=2**63 - 1)]
) -> NodeUpgradeInventory:
    observed = await invoke(control(request).observed)
    if observed is None:
        raise problem(UpgradeUnavailable("Node runtime observation is unavailable"))
    try:
        return await maintenance(request).inventory_async(
            job_id, generation, observed.runtime.environment_sha256
        )
    except CoireError as error:
        raise problem(error) from error
