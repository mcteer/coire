"""Role-gated aggregate state and live reconciliation for the admin SPA."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from opentelemetry import metrics, trace
from sqlalchemy import and_, or_, select

from coire_api.auth import CurrentAdmin
from coire_api.console.ops import answer_from_snapshot
from coire_api.console.service import project_snapshot
from coire_api.db import DownloadJobRow, ModelInstanceRow, ModelRow
from coire_api.deps import SessionDep, SettingsDep
from coire_core.models.console import (
    ActivityItem,
    ActivityKind,
    AskRequest,
    AskResponse,
    ConsoleEvent,
    ConsoleEventKind,
    ConsoleSnapshot,
    CursorPage,
)
from coire_core.models.instance import TERMINAL_INSTANCE_STATES
from coire_core.models.jobs import DownloadStage

router = APIRouter(prefix="/api/v1/admin", tags=["admin: console"])
_tracer = trace.get_tracer(__name__)
_meter = metrics.get_meter(__name__)
_snapshots = _meter.create_counter("coire_console_snapshots_total")
_streams = _meter.create_counter("coire_console_stream_connections_total")
_asks = _meter.create_counter("coire_console_ask_total")
_activity_pages = _meter.create_counter("coire_console_activity_pages_total")


@router.get("/console", response_model=ConsoleSnapshot)
async def console_snapshot(
    request: Request, principal: CurrentAdmin, session: SessionDep, settings: SettingsDep
) -> ConsoleSnapshot:
    with _tracer.start_as_current_span("coire.api.console.snapshot"):
        result = await project_snapshot(request, principal, session, settings)
        _snapshots.add(1)
        return result


@router.get("/console/activity", response_model=CursorPage[ActivityItem])
async def console_activity(
    principal: CurrentAdmin,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before: datetime | None = None,
    before_id: uuid.UUID | None = None,
) -> CursorPage[ActivityItem]:
    """Return all currently shipped activity kinds in one bounded timeline."""
    with _tracer.start_as_current_span("coire.api.console.activity"):
        now = datetime.now(UTC)
        job_query = select(DownloadJobRow).order_by(
            DownloadJobRow.started_at.desc(), DownloadJobRow.id.desc()
        )
        instance_query = select(ModelInstanceRow).order_by(
            ModelInstanceRow.created_at.desc(), ModelInstanceRow.id.desc()
        )
        if before is not None:
            job_query = job_query.where(
                or_(
                    DownloadJobRow.started_at < before,
                    and_(DownloadJobRow.started_at == before, DownloadJobRow.id < before_id),
                )
                if before_id is not None
                else DownloadJobRow.started_at < before
            )
            instance_query = instance_query.where(
                or_(
                    ModelInstanceRow.created_at < before,
                    and_(ModelInstanceRow.created_at == before, ModelInstanceRow.id < before_id),
                )
                if before_id is not None
                else ModelInstanceRow.created_at < before
            )
        jobs = (await session.execute(job_query.limit(limit + 1))).scalars().all()
        instances = (await session.execute(instance_query.limit(limit + 1))).scalars().all()
        model_ids = {row.model_id for row in jobs} | {row.model_id for row in instances}
        models = {
            row.id: row.display_name
            for row in (await session.execute(select(ModelRow).where(ModelRow.id.in_(model_ids))))
            .scalars()
            .all()
        }
    items = [
        ActivityItem(
            id=row.id,
            kind=ActivityKind.JOB,
            owner="platform",
            target=models.get(row.model_id, str(row.model_id)),
            state=row.stage.value,
            started_at=row.started_at,
            elapsed_seconds=max(0, (now - row.started_at).total_seconds()),
            progress_percent=(row.bytes_done / row.bytes_total * 100) if row.bytes_total else 0,
            failure_reason=row.failure_reason,
            can_stop=row.stage not in {DownloadStage.DONE, DownloadStage.FAILED},
        )
        for row in jobs
    ] + [
        ActivityItem(
            id=row.id,
            kind=ActivityKind.INSTANCE,
            owner="platform",
            target=models.get(row.model_id, str(row.model_id)),
            state=row.state.value,
            started_at=row.created_at,
            elapsed_seconds=max(0, (now - row.created_at).total_seconds()),
            failure_reason=row.failure_detail,
            can_stop=row.state not in TERMINAL_INSTANCE_STATES,
        )
        for row in instances
    ]
    items.sort(key=lambda item: (item.started_at, item.id), reverse=True)
    page = items[:limit]
    next_cursor = f"{page[-1].started_at.isoformat()}|{page[-1].id}" if len(items) > limit else None
    _activity_pages.add(1, {"has_next": str(next_cursor is not None).lower()})
    return CursorPage(items=page, next_cursor=next_cursor)


@router.get("/console/events", response_class=StreamingResponse)
async def console_events(
    request: Request,
    principal: CurrentAdmin,
    settings: SettingsDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    _streams.add(1, {"reconnect": str(last_event_id is not None).lower()})

    async def stream() -> AsyncIterator[str]:
        previous = ""
        first = True
        while not await request.is_disconnected():
            from coire_api.db import session_scope

            async with session_scope() as session:
                snapshot = await project_snapshot(request, principal, session, settings)
            body = snapshot.model_dump_json()
            if first or body != previous:
                kind = (
                    ConsoleEventKind.RECONCILE
                    if first and last_event_id
                    else ConsoleEventKind.SNAPSHOT
                )
                event = ConsoleEvent(
                    id=snapshot.cursor,
                    kind=kind,
                    observed_at=snapshot.observed_at,
                    snapshot=snapshot,
                )
                yield f"id: {event.id}\nevent: {event.kind.value}\ndata: {event.model_dump_json()}\n\n"
                previous = body
                first = False
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(settings.instance_event_poll_interval_s)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/ops/ask", response_model=AskResponse)
async def ask_coire(
    body: AskRequest,
    request: Request,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> AskResponse:
    with _tracer.start_as_current_span("coire.api.console.ask"):
        snapshot = await project_snapshot(request, principal, session, settings)
    answer = answer_from_snapshot(snapshot)
    _asks.add(1, {"outcome": answer.status.value})
    return answer
