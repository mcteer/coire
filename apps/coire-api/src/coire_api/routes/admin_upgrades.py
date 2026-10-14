"""Human-admin upgrade catalog and durable status; paths stay in trusted stores."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from sqlalchemy import select

from coire_api.db import UpgradeReleaseRow, session_factory, session_scope
from coire_api.upgrades.authorization import UpgradeAdmin, authorize
from coire_api.upgrades.events import event_page
from coire_api.upgrades.persistence import get_operation, register_release
from coire_core.errors import UpgradeForbidden, UpgradeNotFound, UpgradeUnavailable
from coire_core.models.upgrade_native_terminal import (
    UpgradeNativeTerminalGrant,
    UpgradeNativeTerminalRequest,
)
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryCancellation,
    UpgradeExpiredRecoveryCancelRequest,
    UpgradeExpiredRecoveryIntent,
    UpgradeExpiredRecoveryObservation,
    UpgradeExpiredRecoveryRequest,
    UpgradeExpiredRecoveryStatus,
    UpgradeExpiredRecoveryUnstartedCloseRequest,
    UpgradeExpiredRecoveryUnstartedClosure,
    UpgradeRecoveryHostHoldReceipt,
    UpgradeRecoveryHostHoldRequest,
    UpgradeRecoverySchemaObservation,
    UpgradeRecoverySchemaReceipt,
    UpgradeRecoverySchemaRequest,
)
from coire_core.models.upgrade_recovery_continuation import (
    UpgradeColdContinuationRead,
    UpgradeColdContinuationRequest,
    UpgradeColdContinuationSchemaObservation,
    UpgradeColdContinuationSchemaReceipt,
    UpgradeColdContinuationSchemaRequest,
)
from coire_core.models.upgrade_unclaimed import (
    UpgradeUnclaimedCancellationGrant,
    UpgradeUnclaimedCancellationRequest,
)
from coire_core.models.upgrades import (
    TERMINAL_UPGRADE_PHASES,
    CoreHandoverStartRequest,
    CoreHandoverSummary,
    StudioName,
    UpgradeCancelRequest,
    UpgradeEventPage,
    UpgradeId,
    UpgradeKind,
    UpgradeOperation,
    UpgradeRelease,
    UpgradeReleasePage,
    UpgradeReleaseRegister,
    UpgradeReleaseSummary,
    UpgradeRollbackRequest,
    UpgradeStartRequest,
    UpgradeTargetList,
)
from coire_core.upgrade_artifacts import verify_prepared_release

router = APIRouter(prefix="/api/v1/admin/upgrades", tags=["admin:upgrades"])
tracer = trace.get_tracer("coire.api.upgrades")


@router.post("/recoveries/{recovery_id}/host-hold", response_model=UpgradeRecoveryHostHoldReceipt)
async def reconcile_recovered_host_hold(
    recovery_id: UpgradeId,
    body: UpgradeRecoveryHostHoldRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeRecoveryHostHoldReceipt:
    from coire_api.upgrades.expired_recovery_host_hold import reconcile_host_hold, reconciliations

    enabled(request, response)
    async with session_scope() as session:
        receipt, created = await reconcile_host_hold(
            session, principal, request.app.state.settings, recovery_id, body
        )
    if created:
        reconciliations.add(1, {"outcome": "released"})
    return receipt


def enabled(request: Request, response: Response) -> None:
    if not request.app.state.settings.upgrades_enabled:
        raise UpgradeUnavailable("Managed upgrades are disabled")
    response.headers["Cache-Control"] = "no-store"


@router.get("/recovery-schema/{job_id}", response_model=UpgradeRecoverySchemaObservation)
async def recovery_schema_observation(
    job_id: UpgradeId, request: Request, response: Response, principal: UpgradeAdmin
) -> UpgradeRecoverySchemaObservation:
    from coire_api.upgrades.recovery_schema import inspect_legacy_schema

    enabled(request, response)
    async with session_scope() as session:
        return await inspect_legacy_schema(session, principal, job_id)


@router.post("/recovery-schema", response_model=UpgradeRecoverySchemaReceipt)
async def recovery_schema_preparation(
    body: UpgradeRecoverySchemaRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeRecoverySchemaReceipt:
    from coire_api.upgrades.recovery_schema import prepare_legacy_schema

    enabled(request, response)
    async with session_scope() as session:
        receipt, _ = await prepare_legacy_schema(session, principal, body)
    return receipt


@router.post("/recoveries", response_model=UpgradeExpiredRecoveryIntent, status_code=202)
async def accept_recovery(
    body: UpgradeExpiredRecoveryRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeExpiredRecoveryIntent:
    from coire_api.upgrades.expired_recovery_initial_relay import InitialRecoveryObservationRelay
    from coire_api.upgrades.expired_recovery_orchestration import accept_with_observation

    enabled(request, response)
    relay: InitialRecoveryObservationRelay = request.app.state.upgrade_initial_recovery_observations

    async def observe(
        selector: UpgradeExpiredRecoveryRequest, support: UpgradeRelease
    ) -> UpgradeExpiredRecoveryObservation:
        return relay.read(selector, support)

    result, created = await accept_with_observation(session_factory(), principal, body, observe)
    response.status_code = 202 if created else 200
    response.headers["Location"] = f"/api/v1/admin/upgrades/recoveries/{result.recovery_id}"
    return result


@router.get("/recoveries/{recovery_id}", response_model=UpgradeExpiredRecoveryStatus)
async def recovery_status(
    recovery_id: UpgradeId,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeExpiredRecoveryStatus:
    from coire_api.upgrades.expired_recovery_status import read_recovery_status

    enabled(request, response)
    with tracer.start_as_current_span("coire.api.upgrades_recovery_read"):
        async with session_scope() as session:
            return await read_recovery_status(session, principal, recovery_id)


@router.post("/recoveries/{recovery_id}/cancel", response_model=UpgradeExpiredRecoveryCancellation)
async def cancel_expired_recovery(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryCancelRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeExpiredRecoveryCancellation:
    from coire_api.upgrades.expired_recovery_cancel import cancel_recovery, record_cancellation

    enabled(request, response)
    async with session_scope() as session:
        result, created = await cancel_recovery(session, principal, recovery_id, body)
    record_cancellation(result, created)
    return result


@router.post(
    "/recoveries/{recovery_id}/close-unstarted",
    response_model=UpgradeExpiredRecoveryUnstartedClosure,
)
async def close_unstarted_recovery(
    recovery_id: UpgradeId,
    body: UpgradeExpiredRecoveryUnstartedCloseRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeExpiredRecoveryUnstartedClosure:
    from coire_api.upgrades.expired_recovery_unstarted import close_unstarted_recovery as close
    from coire_api.upgrades.expired_recovery_unstarted import record_unstarted_closure

    enabled(request, response)
    async with session_scope() as session:
        result, created = await close(
            session,
            principal,
            recovery_id,
            body,
            request.app.state.upgrade_unstarted_recovery_observations,
        )
    record_unstarted_closure(result, created)
    return result


@router.post("/executor-handover", response_model=CoreHandoverSummary, status_code=202)
async def start_executor_handover(
    body: CoreHandoverStartRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> CoreHandoverSummary:
    from coire_api.upgrades.handover_authority import start_handover

    enabled(request, response)
    async with session_scope() as session:
        result, created = await start_handover(session, principal, body, request.app.state.settings)
    response.status_code = 202 if created else 200
    response.headers["Location"] = f"/api/v1/admin/upgrades/executor-handover/{result.handover_id}"
    return result


@router.get("/executor-handover/{handover_id}", response_model=CoreHandoverSummary)
async def executor_handover_status(
    handover_id: UUID, request: Request, response: Response, principal: UpgradeAdmin
) -> CoreHandoverSummary:
    from coire_api.db import UpgradeHandoverRow
    from coire_api.upgrades.handover_authority import handover_summary

    enabled(request, response)
    async with session_scope() as session:
        await authorize(session, principal)
        row = await session.get(UpgradeHandoverRow, handover_id)
        if row is None or row.project != request.app.state.settings.upgrade_project:
            raise UpgradeNotFound()
        return handover_summary(row)


@router.post("/releases", response_model=UpgradeReleaseSummary, status_code=201)
@tracer.start_as_current_span(
    "coire.api.upgrade_register", record_exception=False, set_status_on_exception=False
)
async def register(
    body: UpgradeReleaseRegister, request: Request, response: Response, principal: UpgradeAdmin
) -> UpgradeReleaseSummary:
    enabled(request, response)
    release = await asyncio.to_thread(
        verify_prepared_release, Path(request.app.state.settings.upgrade_prepared_dir), body
    )
    async with session_scope() as session:
        result = await register_release(session, principal, release)
    return UpgradeReleaseSummary.from_release(result)


@router.get("/releases", response_model=UpgradeReleasePage)
@tracer.start_as_current_span(
    "coire.api.upgrade_releases", record_exception=False, set_status_on_exception=False
)
async def releases(
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
    cursor: UUID | None = None,
    kind: UpgradeKind | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> UpgradeReleasePage:
    enabled(request, response)
    async with session_scope() as session:
        await authorize(session, principal)
        statement = select(UpgradeReleaseRow)
        if cursor is not None:
            statement = statement.where(UpgradeReleaseRow.id > cursor)
        if kind is not None:
            statement = statement.where(
                UpgradeReleaseRow.payload["manifest"]["kind"].astext == kind.value
            )
        rows = list(
            (await session.scalars(statement.order_by(UpgradeReleaseRow.id).limit(limit + 1))).all()
        )
        return UpgradeReleasePage(
            items=tuple(
                UpgradeReleaseSummary.from_release(UpgradeRelease.model_validate(row.payload))
                for row in rows[:limit]
            ),
            next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
        )


@router.get("/targets", response_model=UpgradeTargetList)
async def targets(
    request: Request, response: Response, principal: UpgradeAdmin
) -> UpgradeTargetList:
    from coire_api.upgrades.targets import target_statuses

    enabled(request, response)
    async with session_scope() as session:
        await authorize(session, principal)
        return await target_statuses(session, request.app.state.settings)


@router.get("/{job_id}", response_model=UpgradeOperation)
@tracer.start_as_current_span(
    "coire.api.upgrade_status", record_exception=False, set_status_on_exception=False
)
async def operation(
    job_id: UpgradeId, request: Request, response: Response, principal: UpgradeAdmin
) -> UpgradeOperation:
    enabled(request, response)
    async with session_scope() as session:
        await authorize(session, principal)
        return UpgradeOperation.model_validate((await get_operation(session, job_id)).payload)


@router.post("/nodes/{node_id}", response_model=UpgradeOperation, status_code=202)
@tracer.start_as_current_span(
    "coire.api.upgrade_accept_studio", record_exception=False, set_status_on_exception=False
)
async def start_studio(
    node_id: StudioName,
    body: UpgradeStartRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeOperation:
    from coire_api.upgrades.acceptance import accept_studio

    enabled(request, response)
    async with session_scope() as session:
        result, created = await accept_studio(
            session, principal, node_id, body, request.app.state.settings
        )
    response.status_code = 202 if created else 200
    response.headers["Location"] = f"/api/v1/admin/upgrades/{result.job_id}"
    return result


@router.post("/control-plane", response_model=UpgradeOperation, status_code=202)
@tracer.start_as_current_span(
    "coire.api.upgrade_accept_core", record_exception=False, set_status_on_exception=False
)
async def start_core(
    body: UpgradeStartRequest, request: Request, response: Response, principal: UpgradeAdmin
) -> UpgradeOperation:
    from coire_api.upgrades.acceptance import accept_core

    enabled(request, response)
    async with session_scope() as session:
        result, created = await accept_core(session, principal, body, request.app.state.settings)
    response.status_code = 202 if created else 200
    response.headers["Location"] = f"/api/v1/admin/upgrades/{result.job_id}"
    return result


@router.post("/{job_id}/rollback", response_model=UpgradeOperation, status_code=202)
@tracer.start_as_current_span(
    "coire.api.upgrade_rollback", record_exception=False, set_status_on_exception=False
)
async def rollback_operation(
    job_id: UpgradeId,
    body: UpgradeRollbackRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeOperation:
    from coire_api.upgrades.acceptance import accept_rollback

    enabled(request, response)
    async with session_scope() as session:
        operation, created = await accept_rollback(
            session, principal, job_id, body, request.app.state.settings
        )
    response.status_code = 202 if created else 200
    response.headers["Location"] = f"/api/v1/admin/upgrades/{operation.job_id}"
    return operation


@router.get("/{job_id}/events", response_model=UpgradeEventPage)
async def events(
    job_id: UpgradeId,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
    after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    last_event_id: Annotated[str | None, Header(max_length=19)] = None,
) -> UpgradeEventPage | StreamingResponse:
    enabled(request, response)
    if last_event_id is not None:
        if (
            not last_event_id.isascii()
            or not last_event_id.isdigit()
            or int(last_event_id) > 2**63 - 1
        ):
            raise HTTPException(422, "Invalid Last-Event-ID")
        after = int(last_event_id)

    async def page(cursor: int) -> UpgradeEventPage:
        with tracer.start_as_current_span(
            "coire.api.upgrade_events", record_exception=False, set_status_on_exception=False
        ):
            async with session_scope() as session:
                await authorize(session, principal)
                return await event_page(session, job_id, after=cursor, limit=limit)

    initial = await page(after)
    if "text/event-stream" not in request.headers.get("accept", ""):
        return initial

    async def stream() -> AsyncIterator[str]:
        current = initial
        while not await request.is_disconnected():
            for event in current.events:
                yield f"id: {event.sequence}\nevent: progress\ndata: {event.model_dump_json()}\n\n"
            kind = "reset" if current.reset else "snapshot"
            yield f"id: {current.cursor}\nevent: {kind}\ndata: {current.model_dump_json()}\n\n"
            if (
                current.operation.phase in TERMINAL_UPGRADE_PHASES
                and current.cursor >= current.operation.receipt_sequence
            ):
                return
            if len(current.events) < limit:
                await asyncio.sleep(1)
            try:
                current = await page(current.cursor)
            except (UpgradeForbidden, UpgradeNotFound):
                return
            yield ": heartbeat\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/cancel", response_model=UpgradeOperation, status_code=202)
@tracer.start_as_current_span(
    "coire.api.upgrade_cancel", record_exception=False, set_status_on_exception=False
)
async def cancel_operation(
    job_id: UpgradeId,
    body: UpgradeCancelRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeOperation:
    from coire_api.upgrades.service import request_cancel

    enabled(request, response)
    async with session_scope() as session:
        return await request_cancel(
            session, principal, job_id, body, settings=request.app.state.settings
        )


@router.post(
    "/recoveries/continuations", response_model=UpgradeExpiredRecoveryIntent, status_code=202
)
async def cold_continuation_start(
    body: UpgradeColdContinuationRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeExpiredRecoveryIntent:
    from coire_api.upgrades.cold_continuation_accept import accept_continuation

    enabled(request, response)
    async with session_scope() as session:
        intent, created = await accept_continuation(
            session, principal, body, request.app.state.upgrade_cold_continuation_observations
        )
    from coire_api.upgrades.cold_continuation_telemetry import record_acceptance

    record_acceptance(intent, created)
    if not created:
        response.status_code = 200
    return intent


@router.post(
    "/recoveries/{recovery_id}/continuation-schema-observation",
    response_model=UpgradeColdContinuationSchemaObservation,
)
async def cold_continuation_schema_observation(
    recovery_id: UpgradeId,
    body: UpgradeColdContinuationRead,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeColdContinuationSchemaObservation:
    from coire_api.upgrades.cold_continuation_schema import observe_schema

    enabled(request, response)
    async with session_scope() as session:
        return await observe_schema(session, principal, recovery_id, body)


@router.post(
    "/recoveries/{recovery_id}/continuation-schema",
    response_model=UpgradeColdContinuationSchemaReceipt,
)
async def cold_continuation_schema_preparation(
    recovery_id: UpgradeId,
    body: UpgradeColdContinuationSchemaRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeColdContinuationSchemaReceipt:
    from coire_api.upgrades.cold_continuation_schema import prepare_schema
    from coire_core.errors import UpgradeConflict

    enabled(request, response)
    if body.previous_recovery_id != recovery_id:
        raise UpgradeConflict("Cold continuation schema path differs")
    async with session_scope() as session:
        receipt, created = await prepare_schema(session, principal, body)
    from coire_api.upgrades.cold_continuation_telemetry import record_schema

    record_schema(receipt, created)
    return receipt


@router.post("/{job_id}/unclaimed-cancellation", response_model=UpgradeUnclaimedCancellationGrant)
async def prepare_unclaimed_cancellation(
    job_id: UpgradeId,
    body: UpgradeUnclaimedCancellationRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeUnclaimedCancellationGrant:
    from coire_api.upgrades.unclaimed_cancellation import request_unclaimed_cancellation, requests

    enabled(request, response)
    async with session_scope() as session:
        receipt = await request_unclaimed_cancellation(session, principal, job_id, body)
    requests.add(1, {"outcome": "accepted"})
    return receipt


@router.post("/recoveries/{recovery_id}/native-terminal", response_model=UpgradeNativeTerminalGrant)
async def prepare_legacy_native_terminal(
    recovery_id: UpgradeId,
    body: UpgradeNativeTerminalRequest,
    request: Request,
    response: Response,
    principal: UpgradeAdmin,
) -> UpgradeNativeTerminalGrant:
    from coire_api.upgrades.native_terminal import prepare_native_terminal, requests

    enabled(request, response)
    async with session_scope() as session:
        result = await prepare_native_terminal(session, principal, recovery_id, body)
    requests.add(1, {"outcome": "accepted"})
    return result
