"""Private authenticated exact-instance generation for measured Studio workloads.

The context is task-local, never accepted from a public request. The proxy rechecks
it inside the same node admission locks used by ordinary request leases.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    aclosing,
    asynccontextmanager,
    contextmanager,
    suppress,
)
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import cast

from fastapi import Request
from opentelemetry import trace
from sqlalchemy import Table, Text, bindparam, func, insert, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import SessionTransaction, load_only
from sqlalchemy.orm.attributes import set_committed_value

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import (
    ApiKeyRow,
    MemoryReservationRow,
    ModelInstanceRow,
    NodeRow,
    RequestLeaseRow,
    TrainingCommandRow,
    TrainingMeasurementRow,
    UserRow,
    session_scope,
)
from coire_api.gateway.execution import canonical_text_payload, track_stream
from coire_api.gateway.proxy import StreamTiming, stream
from coire_api.gateway.resolution import ResolvedModel
from coire_api.gateway.usage import UsageTracker
from coire_api.identity.limits import (
    KeyAdmissionWindow,
    MonthlyQuotaExceeded,
    RateLimitExceeded,
    locked_key_admission_cte,
    raise_locked_limit_refusal,
)
from coire_api.placement.service import lock_nodes_for_admission
from coire_api.training.authorization import preflight_training_action
from coire_api.training.gateway_database import record_reconnect
from coire_api.training.measurement_residents import MeasurementResident
from coire_api.training.measurements import validate_measurement_prompts
from coire_api.training.service import payload_digest
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict, TrainingForbidden
from coire_core.models.auth import UserRole
from coire_core.models.gateway import ChatMessage, GatewayProtocol, UsageOutcome
from coire_core.models.training import (
    TrainingMeasurementCompletion,
    TrainingMeasurementPrompt,
    TrainingMeasurementPromptSet,
    TrainingMeasurementRequest,
    TrainingResidentTarget,
)
from coire_core.models.training_node import TrainingMeasurementDispatch
from coire_core.settings import Settings

_gateway_request: ContextVar[tuple[Request, float, AsyncSession] | None] = ContextVar(
    "measurement_gateway_request", default=None
)


class _DisconnectRequest(Request):
    def __init__(self, request: Request, disconnected: asyncio.Event) -> None:
        super().__init__(request.scope)
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self.disconnected.is_set()


@asynccontextmanager
async def measured_gateway_request(
    request: Request, started_at: float, session: AsyncSession
) -> AsyncIterator[None]:
    """Observe disconnect once after body parsing; retain checks on every frame."""
    disconnected = asyncio.Event()

    async def watch_disconnect() -> None:
        try:
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    return
        except Exception:
            # A broken receive channel cannot authorize delivery. Never log its
            # exception/body: only the static failure is operational metadata.
            logging.getLogger(__name__).warning("measurement disconnect receive failed")
            disconnected.set()

    watcher = asyncio.create_task(watch_disconnect())
    guarded_request = _DisconnectRequest(request, disconnected)
    token = _gateway_request.set((guarded_request, started_at, session))
    try:
        yield
    finally:
        _gateway_request.reset(token)
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


@asynccontextmanager
async def _measurement_admission_session() -> AsyncIterator[AsyncSession]:
    http_request = _gateway_request.get()
    if http_request is not None:
        session = http_request[2]
        if not session.in_transaction():
            raise TrainingConflict("Measurement route transaction ended")
        yield session
    else:
        async with session_scope() as session:
            yield session


@asynccontextmanager
async def measurement_recheck_session(
    fallback: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> AsyncIterator[AsyncSession]:
    """Open an independent fresh transaction using the private route's bounded pool."""
    http_request = _gateway_request.get()
    if http_request is None:
        async with fallback() as session:
            yield session
    else:
        # Parallel credential checks must never share the admission AsyncSession.
        async with AsyncSession(bind=http_request[2].bind, expire_on_commit=False) as session:
            yield session
            await session.commit()


@dataclass(frozen=True)
class _FrozenWorkload:
    request_document_sha256: bytes
    command_document_sha256: bytes
    request_sha256: str
    request: TrainingMeasurementRequest
    prompts: TrainingMeasurementPromptSet
    dispatch: TrainingMeasurementDispatch
    principal: Principal
    node_bindings: tuple[tuple[uuid.UUID, str], ...] | None = None

    def matches(
        self, command: TrainingCommandRow, request_sha256: bytes, command_sha256: bytes
    ) -> bool:
        return (
            self.request_document_sha256 == request_sha256
            and self.command_document_sha256 == command_sha256
            and self.request_sha256 == command.request_sha256
        )


@dataclass(frozen=True)
class MeasurementGateway:
    """Immutable routing identity; every call independently checks live authority."""

    generate: Callable[
        [Principal, uuid.UUID, TrainingResidentTarget, TrainingMeasurementPrompt, int],
        Awaitable[TrainingMeasurementCompletion],
    ]
    workloads: OrderedDict[uuid.UUID, _FrozenWorkload]

    def routing_principal(
        self, identity: uuid.UUID, node: str, principal_sha256: str
    ) -> Principal | None:
        workload = self.workloads.get(identity)
        if (
            workload is None
            or node not in workload.request.nodes
            or payload_digest(workload.principal) != principal_sha256
        ):
            return None
        return workload.principal

    async def __call__(
        self,
        principal: Principal,
        measurement_id: uuid.UUID,
        target: TrainingResidentTarget,
        prompt: TrainingMeasurementPrompt,
        max_output_tokens: int,
    ) -> TrainingMeasurementCompletion:
        return await self.generate(principal, measurement_id, target, prompt, max_output_tokens)


_parsed_workloads: ContextVar[OrderedDict[uuid.UUID, _FrozenWorkload] | None] = ContextVar(
    "measurement_parsing", default=None
)


@contextmanager
def _workload_parsing(cache: OrderedDict[uuid.UUID, _FrozenWorkload]) -> Iterator[None]:
    token = _parsed_workloads.set(cache)
    try:
        yield
    finally:
        _parsed_workloads.reset(token)


@dataclass(frozen=True)
class _MeasurementScope:
    principal: Principal
    measurement_id: uuid.UUID
    target: TrainingResidentTarget
    prompt: TrainingMeasurementPrompt
    max_output_tokens: int
    engine_url: str
    admission_session: AsyncSession | None = None
    admission_transaction: SessionTransaction | None = None
    resident: MeasurementResident | None = None
    workload: _FrozenWorkload | None = None


_scope: ContextVar[_MeasurementScope | None] = ContextVar("gateway_measurement", default=None)


# Reuse SQL structures, never query results or live authority.
# PostgreSQL computes SHA-256 over both complete fresh stored documents. Warm
# reads return only these digests, never reuse a live authorization result.
_MEASUREMENT_DOCUMENT_HASHES = (
    func.sha256(func.convert_to(sql_cast(TrainingMeasurementRow.request, Text), "UTF8")),
    func.sha256(func.convert_to(sql_cast(TrainingCommandRow.payload, Text), "UTF8")),
)
_MEASUREMENT_COMMAND_BASE = (
    select(TrainingMeasurementRow, TrainingCommandRow, UserRow, *_MEASUREMENT_DOCUMENT_HASHES)
    .select_from(TrainingMeasurementRow)
    .outerjoin(
        TrainingCommandRow,
        (TrainingCommandRow.subject_id == bindparam("measurement_subject"))
        & (TrainingCommandRow.operation == "training.measurement"),
    )
    .join(UserRow, UserRow.id == bindparam("measurement_owner"))
    .where(TrainingMeasurementRow.id == bindparam("measurement_id"))
    .options(load_only(UserRow.id, UserRow.active, UserRow.role, raiseload=True))
    .execution_options(populate_existing=True)
)
_MEASUREMENT_COMMAND = _MEASUREMENT_COMMAND_BASE.options(
    load_only(
        TrainingMeasurementRow.id,
        TrainingMeasurementRow.owner_user_id,
        TrainingMeasurementRow.state,
        TrainingMeasurementRow.request,
        raiseload=True,
    ),
    load_only(
        TrainingCommandRow.id,
        TrainingCommandRow.actor_user_id,
        TrainingCommandRow.payload,
        TrainingCommandRow.request_sha256,
        raiseload=True,
    ),
)
_MEASUREMENT_CACHED_COMMAND = _MEASUREMENT_COMMAND_BASE.options(
    load_only(
        TrainingMeasurementRow.id,
        TrainingMeasurementRow.owner_user_id,
        TrainingMeasurementRow.state,
        raiseload=True,
    ),
    load_only(
        TrainingCommandRow.id,
        TrainingCommandRow.actor_user_id,
        TrainingCommandRow.request_sha256,
        raiseload=True,
    ),
)

_MEASUREMENT_NODES = (
    select(NodeRow)
    .where(NodeRow.name.in_(bindparam("measurement_nodes", expanding=True)))
    .options(load_only(NodeRow.id, NodeRow.name, raiseload=True))
)
_MEASUREMENT_OWNER = _MEASUREMENT_COMMAND.with_for_update(of=UserRow, read=True)
_MEASUREMENT_KEY_OPTIONS = (
    load_only(
        ApiKeyRow.id,
        ApiKeyRow.user_id,
        ApiKeyRow.revoked_at,
        ApiKeyRow.credential_version,
        ApiKeyRow.scopes,
        ApiKeyRow.requests_per_minute,
        ApiKeyRow.monthly_budget_tokens,
        raiseload=True,
    ),
)
_MEASUREMENT_KEY_OWNER = (
    _MEASUREMENT_COMMAND.add_columns(ApiKeyRow)
    .join(ApiKeyRow, ApiKeyRow.id == bindparam("measurement_key"))
    .options(*_MEASUREMENT_KEY_OPTIONS)
    .with_for_update(of=[UserRow, ApiKeyRow], read=True)
)
_MEASUREMENT_CACHED_OWNER = _MEASUREMENT_CACHED_COMMAND.with_for_update(of=UserRow, read=True)
_MEASUREMENT_CACHED_KEY_OWNER = (
    _MEASUREMENT_CACHED_COMMAND.add_columns(ApiKeyRow)
    .join(ApiKeyRow, ApiKeyRow.id == bindparam("measurement_key"))
    .options(*_MEASUREMENT_KEY_OPTIONS)
    .with_for_update(of=[UserRow, ApiKeyRow], read=True)
)


_LEASE_HOLD_UPDATE = (
    update(MemoryReservationRow)
    .where(MemoryReservationRow.id == bindparam("coire_lease_hold"))
    .values(last_used_at=bindparam("coire_lease_now"))
)
_LEASE_HOLD_USED = _LEASE_HOLD_UPDATE.cte("measurement_hold_used")
_LEASE_INSTANCE_UPDATE = (
    update(ModelInstanceRow)
    .where(ModelInstanceRow.id == bindparam("coire_lease_instance"))
    .values(in_flight=ModelInstanceRow.in_flight + 1)
    .returning(ModelInstanceRow.in_flight)
)
_LEASE_INSTANCE_USED = _LEASE_INSTANCE_UPDATE.cte("measurement_instance_used")
_MEASUREMENT_LEASE = (
    insert(cast(Table, RequestLeaseRow.__table__))
    .values(
        id=bindparam("coire_lease_id"),
        reservation_id=bindparam("coire_lease_hold"),
        request_id=bindparam("coire_lease_request"),
        expires_at=bindparam("coire_lease_deadline"),
    )
    .add_cte(_LEASE_HOLD_USED, _LEASE_INSTANCE_USED)
    .returning(select(_LEASE_INSTANCE_USED.c.in_flight).scalar_subquery())
)


# All live owner/key, node and inventory checks precede these writes and hold
# their original locks. A refused counter admission yields no lease or updates.
_KEY_ADMITTED = locked_key_admission_cte("measurement_key_admitted")
_KEY_ADMITTED_EXISTS = select(_KEY_ADMITTED.c.requests).exists()
_KEY_LEASE_HOLD_USED = _LEASE_HOLD_UPDATE.where(_KEY_ADMITTED_EXISTS).cte("measurement_hold_used")
_KEY_LEASE_INSTANCE_USED = _LEASE_INSTANCE_UPDATE.where(_KEY_ADMITTED_EXISTS).cte(
    "measurement_instance_used"
)
_REQUEST_LEASE_TABLE = cast(Table, RequestLeaseRow.__table__)
_KEY_MEASUREMENT_LEASE = (
    insert(_REQUEST_LEASE_TABLE)
    .from_select(
        ["id", "reservation_id", "request_id", "expires_at"],
        select(
            bindparam("coire_lease_id", type_=_REQUEST_LEASE_TABLE.c.id.type),
            bindparam("coire_lease_hold", type_=_REQUEST_LEASE_TABLE.c.reservation_id.type),
            bindparam("coire_lease_request", type_=_REQUEST_LEASE_TABLE.c.request_id.type),
            bindparam("coire_lease_deadline", type_=_REQUEST_LEASE_TABLE.c.expires_at.type),
        ).select_from(_KEY_ADMITTED),
    )
    .add_cte(_KEY_LEASE_HOLD_USED, _KEY_LEASE_INSTANCE_USED)
    .returning(select(_KEY_LEASE_INSTANCE_USED.c.in_flight).scalar_subquery())
)


@contextmanager
def _admission_timing(stage: str, *, initial_only: bool = True) -> Iterator[None]:
    """Content-free stage timings on the existing measurement span."""
    if initial_only and _scope.get() is not None:
        yield
        return
    began = perf_counter()
    try:
        yield
    finally:
        trace.get_current_span().add_event(
            "coire.api.training.measurement.admission_stage",
            {"stage": stage, "duration_ms": (perf_counter() - began) * 1000},
        )


async def _authorize(
    session: AsyncSession,
    principal: Principal,
    measurement_id: uuid.UUID,
    target: TrainingResidentTarget,
    prompt: TrainingMeasurementPrompt,
    max_output_tokens: int,
) -> ResolvedModel:
    from coire_api.training.measurement_residents import resolve_measurement_residents

    owner = preflight_training_action(principal, method="GET", origin=None, browser_origin="")
    parameters = {
        "measurement_subject": str(measurement_id),
        "measurement_id": measurement_id,
        "measurement_owner": owner,
        "measurement_key": principal.api_key_id,
    }
    cache = _parsed_workloads.get()
    workload = cache.get(measurement_id) if cache is not None else None
    if principal.kind is PrincipalKind.API_KEY:
        statement = (
            _MEASUREMENT_CACHED_KEY_OWNER if workload is not None else _MEASUREMENT_KEY_OWNER
        )
    else:
        statement = _MEASUREMENT_CACHED_OWNER if workload is not None else _MEASUREMENT_OWNER
    retry_read = session.info.pop("coire.measurement.read_retry_allowed", False)
    pair = None
    for read_attempt in range(2):
        try:
            with _admission_timing("authority"):
                pair = (await session.execute(statement, parameters)).one_or_none()
            break
        except DBAPIError as error:
            if read_attempt != 0 or not retry_read or not error.connection_invalidated:
                raise
            # This is the warm route's first readonly query, before admission
            # writes. Never retry node/inventory reads, writes, commits or streams.
            await session.rollback()
            session.info["coire.measurement.read_retry_used"] = True
            record_reconnect(measurement_id)
    if pair is None:
        raise TrainingForbidden()
    row, command, user = pair[:3]
    request_document_sha256, command_document_sha256 = pair[3:5]
    if not user.active or user.role is not UserRole.ADMIN:
        raise TrainingForbidden()
    if principal.kind is PrincipalKind.API_KEY:
        key = pair[5]
        if (
            key.user_id != owner
            or key.revoked_at is not None
            or key.credential_version != principal.credential_version
            or "admin" not in key.scopes
        ):
            raise TrainingForbidden()
        session.info["coire.measurement.api_key"] = key
    if row is None or command is None or row.state != "running" or row.owner_user_id != owner:
        raise TrainingConflict("Measurement gateway authority ended")
    scope = _scope.get()
    if (
        scope is not None
        and scope.workload is not None
        and not scope.workload.matches(command, request_document_sha256, command_document_sha256)
    ):
        raise TrainingConflict("Frozen measurement workload changed during generation")
    if workload is None or not workload.matches(
        command, request_document_sha256, command_document_sha256
    ):
        if workload is not None:
            # A changed document is read again with its fresh database digests.
            # Live owner/key mutation locks remain held in this transaction.
            full_statement = (
                _MEASUREMENT_KEY_OWNER
                if principal.kind is PrincipalKind.API_KEY
                else _MEASUREMENT_OWNER
            )
            full_pair = (await session.execute(full_statement, parameters)).one_or_none()
            if full_pair is None:
                raise TrainingConflict("Measurement gateway authority ended")
            row, command = full_pair[:2]
            request_document_sha256, command_document_sha256 = full_pair[3:5]
            if row.state != "running" or row.owner_user_id != owner:
                raise TrainingConflict("Measurement gateway authority ended")
        request = TrainingMeasurementRequest.model_validate(row.request)
        prompts = TrainingMeasurementPromptSet.model_validate(command.payload.get("prompts"))
        dispatch = TrainingMeasurementDispatch.model_validate(command.payload.get("dispatch"))
        validate_measurement_prompts(request, prompts)
        if command.request_sha256 != payload_digest(request):
            raise TrainingConflict("Generation differs from frozen measured workload")
        workload = _FrozenWorkload(
            request_document_sha256,
            command_document_sha256,
            command.request_sha256,
            request,
            prompts,
            dispatch,
            Principal.model_validate(command.payload["principal"]),
        )
    request, prompts, dispatch = workload.request, workload.prompts, workload.dispatch
    if (
        request.mode != "coexistence"
        or command.actor_user_id != owner
        or workload.principal != principal
        or target not in request.resident_targets
        or prompt not in prompts.prompts
        or max_output_tokens != request.workload.max_output_tokens
        or not dispatch.commands
        or sorted(p.prepare.node for p in dispatch.commands) != sorted(request.nodes)
        or any(
            p.measurement_id != measurement_id
            or p.mode != "coexistence"
            or p.resident_targets != request.resident_targets
            or p.deadline <= datetime.now(UTC)
            for p in dispatch.commands
        )
    ):
        raise TrainingConflict("Generation differs from frozen measured workload")
    node_bindings = workload.node_bindings
    if node_bindings is None:
        nodes = list(
            await session.scalars(_MEASUREMENT_NODES, {"measurement_nodes": request.nodes})
        )
        if len(nodes) != len(request.nodes):
            raise TrainingConflict("Measurement node inventory changed")
        node_bindings = tuple((node.id, node.name) for node in nodes)
    node_ids = [identity for identity, _ in node_bindings]
    with _admission_timing("node_lock"):
        await lock_nodes_for_admission(session, node_ids)
    with _admission_timing("inventory"):
        residents = await resolve_measurement_residents(
            session,
            node_ids,
            request.resident_targets,
            {
                instance_id: engine_id
                for probe in dispatch.commands
                for instance_id, engine_id in probe.resident_engine_ids.items()
            },
            {probe.prepare.reservation_id for probe in dispatch.commands},
            measurement_id,
            dict(node_bindings),
        )
    if workload.node_bindings is None:
        workload = replace(workload, node_bindings=node_bindings)
    if cache is not None:
        cache[measurement_id] = workload
        cache.move_to_end(measurement_id)
        while len(cache) > 8:
            cache.popitem(last=False)
    session.info["coire.measurement.residents"] = residents
    session.info["coire.measurement.workload"] = workload
    return residents[target.instance_id].resolved


@asynccontextmanager
async def measurement_request_session(
    fallback: Callable[[], AbstractAsyncContextManager[AsyncSession]],
) -> AsyncIterator[AsyncSession]:
    """Keep initial authority locks through request-lease acquisition, then commit.

    Only the task-local private measurement can supply this session. Ordinary
    requests and renewals obtain a fresh transaction as before.
    """
    scope = _scope.get()
    session = scope.admission_session if scope is not None else None
    if session is not None and session.in_transaction():
        try:
            yield session
            with _admission_timing("commit", initial_only=False):
                await session.commit()
        except BaseException:
            await session.rollback()
            raise
    else:
        async with fallback() as fresh:
            yield fresh


async def acquire_measurement_request_lease(
    session: AsyncSession, engine_url: str, *, ttl_seconds: float
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Acquire from fresh validated rows while the original admission locks hold.

    This is reachable only through the private task-local measurement scope. Its
    transaction identity must match; no row or authority is reused after commit.
    Ordinary requests and every renewal retain their normal fresh lease checks.
    """
    scope = _scope.get()
    if scope is None or session is not scope.admission_session:
        return None
    if (
        scope.admission_transaction is None
        or session.sync_session.get_transaction() is not scope.admission_transaction
        or not scope.admission_transaction.is_active
        or scope.engine_url != engine_url
        or scope.resident is None
    ):
        raise TrainingConflict("Measurement admission transaction ended")
    resident = scope.resident
    now = datetime.now(UTC)
    lease_id = uuid.uuid4()
    # These rows and the node admission lock remain held in this exact transaction.
    # Lease and key-counter writes execute atomically with one round trip. ORM state is marked
    # committed afterward so no subsequent flush duplicates the counter update.
    parameters: dict[str, object] = {
        "coire_lease_hold": resident.reservation.id,
        "coire_lease_instance": resident.instance.id,
        "coire_lease_now": now,
        "coire_lease_id": lease_id,
        "coire_lease_request": str(uuid.uuid4()),
        "coire_lease_deadline": now + timedelta(seconds=ttl_seconds),
    }
    window = None
    key = None
    if scope.principal.api_key_id is not None:
        # _authorize loaded this fresh row under its mutation lock in this exact
        # transaction. Never reuse a key or counter result after commit.
        key = cast(ApiKeyRow, session.info["coire.measurement.api_key"])
        window = KeyAdmissionWindow.current()
        parameters.update(window.parameters(key))
    with _admission_timing("lease", initial_only=False):
        in_flight = await session.scalar(
            _KEY_MEASUREMENT_LEASE if window is not None else _MEASUREMENT_LEASE,
            parameters,
        )
    if in_flight is None:
        if key is not None and window is not None:
            await raise_locked_limit_refusal(session, key, window)
        raise TrainingConflict("Measurement lease counter update failed")
    set_committed_value(resident.reservation, "last_used_at", now)
    set_committed_value(resident.instance, "in_flight", in_flight)
    return lease_id, resident.instance.id


async def authorize_measurement_lease(session: AsyncSession, engine_url: str) -> None:
    """Proxy-only hook; ordinary admission and guard checks still run afterward."""
    scope = _scope.get()
    if scope is None:
        return
    if session is scope.admission_session and session.in_transaction():
        if scope.engine_url != engine_url:
            raise TrainingConflict("Measurement proxy endpoint changed")
        # Full authority and resident checks already hold their locks in this
        # exact transaction; the proxy now acquires its lease under those locks.
        return
    resolved = await _authorize(
        session,
        scope.principal,
        scope.measurement_id,
        scope.target,
        scope.prompt,
        scope.max_output_tokens,
    )
    if engine_url != scope.engine_url or resolved.engine_url != engine_url:
        raise TrainingConflict("Measurement proxy endpoint changed")


def gateway_measurement_generate(settings: Settings) -> MeasurementGateway:
    """Construct the scheduler's private callback from its configured settings."""

    # Only immutable parsing is memoized. Every use checks database-computed
    # SHA-256 of both complete fresh documents, then rechecks current live authority,
    # deadline, node inventory, registry/artifacts and counted holds.
    cache: OrderedDict[uuid.UUID, _FrozenWorkload] = OrderedDict()

    @observed("coire.api.training.measurement.gateway")
    async def generate(
        principal: Principal,
        measurement_id: uuid.UUID,
        target: TrainingResidentTarget,
        prompt: TrainingMeasurementPrompt,
        max_output_tokens: int,
    ) -> TrainingMeasurementCompletion:
        with _workload_parsing(cache):
            if not settings.training_enabled:
                raise TrainingConflict("Training measurements are disabled")
            began = datetime.now(UTC)
            http_request = _gateway_request.get()
            timing = (
                StreamTiming(request_started_at=http_request[1], upstream_ready=asyncio.Event())
                if http_request is not None
                else StreamTiming()
            )
            async with _measurement_admission_session() as admission_session:
                resolved = await _authorize(
                    admission_session,
                    principal,
                    measurement_id,
                    target,
                    prompt,
                    max_output_tokens,
                )
                assert resolved.engine_url is not None and resolved.model_path is not None
                usage = UsageTracker(
                    principal, str(target.target.model_id), GatewayProtocol.OPENAI, started_at=began
                )
                usage.bind_resolution(resolved)
                scope = _MeasurementScope(
                    principal,
                    measurement_id,
                    target,
                    prompt,
                    max_output_tokens,
                    resolved.engine_url,
                    admission_session,
                    admission_session.sync_session.get_transaction(),
                    admission_session.info["coire.measurement.residents"][target.instance_id],
                    admission_session.info["coire.measurement.workload"],
                )
                token = _scope.set(scope)
                try:
                    payload = canonical_text_payload(
                        [ChatMessage(role="user", content=prompt.text)],
                        resolved.model_path,
                        output_tokens=max_output_tokens,
                    )
                    source = stream(resolved.engine_url, payload, settings, timing)
                    async with aclosing(
                        track_stream(
                            source,
                            usage,
                            request=http_request[0] if http_request is not None else None,
                            timing=timing,
                        )
                    ) as tracked:
                        async for _ in tracked:
                            async with measurement_recheck_session(session_scope) as session:
                                await authorize_measurement_lease(session, resolved.engine_url)
                    if (
                        usage.outcome is not UsageOutcome.SUCCEEDED
                        or not usage.reported_token_usage
                        or usage.first_token_duration_ms is None
                        or usage.instance_id is None
                        or resolved.target is None
                        or usage.prompt_tokens
                        != prompt.tokens_by_instance.get(target.instance_id, prompt.input_tokens)
                        or not 1 <= usage.completion_tokens <= max_output_tokens
                    ):
                        raise TrainingConflict(
                            "Gateway stream lacks exact completed token evidence"
                        )
                    return TrainingMeasurementCompletion(
                        instance_id=usage.instance_id,
                        target=resolved.target,
                        first_token_seconds=usage.first_token_duration_ms / 1000,
                        input_tokens=usage.prompt_tokens,
                        output_tokens=usage.completion_tokens,
                    )
                except (RateLimitExceeded, MonthlyQuotaExceeded):
                    # Refused admission launches no inference and creates no
                    # usage settlement, preserving the pre-batched behavior.
                    if admission_session.in_transaction():
                        await admission_session.rollback()
                    raise
                except BaseException:
                    if admission_session.in_transaction():
                        await admission_session.rollback()
                    await usage.finish(
                        UsageOutcome.FAILED, failure_code="measurement_gateway_failed"
                    )
                    raise
                finally:
                    _scope.reset(token)

    return MeasurementGateway(generate, cache)
