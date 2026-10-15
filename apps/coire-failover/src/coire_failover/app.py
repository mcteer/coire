"""Fail-closed, scoped and bounded inference-only ASGI surface on an elected Studio."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from coire_core.failover_authority import lease_current, lease_expiry, relay_is_current
from coire_core.failover_lifetime import FailoverAuthorityLost, guarded_await
from coire_core.failover_limits import (
    FailoverLimitError,
    IdentityCounters,
    bounded_text_request,
    effective_limits,
)
from coire_core.failover_residency import resident_matches
from coire_core.failover_snapshot import SnapshotError, load_verified_snapshot
from coire_core.failover_telemetry import ingress_refusals_total, snapshot_fresh, tracer
from coire_core.models.failover import (
    FailoverLease,
    FailoverProblem,
    FailoverPublicStatus,
    FailoverRelayAuthority,
    FailoverRelayIssuance,
    FailoverRelayRequest,
    FailoverResidentEngine,
    FailoverSnapshot,
    FailoverStatus,
)
from coire_core.models.gateway import ChatCompletionRequest, GatewayModel, GatewayModelList
from coire_core.settings import Settings, get_settings
from coire_failover.auth import AccessIdentityError, verify_access_assertion
from coire_failover.proxy import completion_relay, fetch_resident, relay_token
from coire_failover.resolution import FailoverModelUnavailable, choose_relay
from coire_failover.tier import project_tier

SnapshotLoader = Callable[[], FailoverSnapshot]
LeaseLoader = Callable[[], FailoverLease | None]
ResidentLoader = Callable[[], Awaitable[list[FailoverResidentEngine]]]
CompletionRelay = Callable[[FailoverRelayRequest, str], Awaitable[Response]]
AuthorityIssuer = Callable[[FailoverRelayIssuance], Awaitable[FailoverRelayAuthority]]
_STATIC_ROOT = Path("/app/static")


class FailoverRuntime:
    def __init__(
        self,
        *,
        load_snapshot: SnapshotLoader | None = None,
        load_lease: LeaseLoader | None = None,
        local_resident: ResidentLoader | None = None,
        peer_resident: ResidentLoader | None = None,
        relay: CompletionRelay | None = None,
        issue_authority: AuthorityIssuer | None = None,
        access_client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.load_snapshot = load_snapshot or _file_snapshot(self.settings)
        self.load_lease = load_lease or _file_lease(self.settings)
        token = relay_token(self.settings)
        self._client = (
            httpx.AsyncClient() if token and (relay is None or issue_authority is None) else None
        )
        self.issue_authority = issue_authority or _authority_issuer(
            self.settings, token, self._client
        )
        self.local_resident = local_resident or _resident_loader(
            self.settings.failover_local_relay_url, token, self._client
        )
        self.peer_resident = peer_resident or _resident_loader(
            self.settings.failover_peer_relay_url, token, self._client
        )
        self.relay = relay or (
            completion_relay(self.settings, self._client) if self._client else None
        )
        self.access_client = access_client
        self.in_flight = 0
        self.counters = IdentityCounters()


def _authority_issuer(
    settings: Settings, token: str, client: httpx.AsyncClient | None
) -> AuthorityIssuer:
    async def issue(body: FailoverRelayIssuance) -> FailoverRelayAuthority:
        if client is None or not settings.failover_local_relay_url:
            raise ValueError("not_elected")
        response = await client.post(
            settings.failover_local_relay_url.rstrip("/") + "/node/failover/authority",
            json=body.model_dump(mode="json"),
            headers={"X-Coire-Failover-Relay": token},
            timeout=1.0,
        )
        response.raise_for_status()
        return FailoverRelayAuthority.model_validate_json(response.content)

    return issue


def _file_snapshot(settings: Settings) -> SnapshotLoader:
    def load() -> FailoverSnapshot:
        try:
            return load_verified_snapshot(
                Path(settings.failover_snapshot_path),
                settings.failover_core_public_key,
                max_age_s=settings.failover_snapshot_max_age_s,
            )
        except SnapshotError as exc:
            raise ValueError("snapshot_unavailable") from exc

    return load


def _file_lease(settings: Settings) -> LeaseLoader:
    def load() -> FailoverLease | None:
        try:
            return FailoverLease.model_validate_json(
                Path(settings.failover_proof_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    return load


def _resident_loader(base_url: str, token: str, client: httpx.AsyncClient | None) -> ResidentLoader:
    async def load() -> list[FailoverResidentEngine]:
        return await fetch_resident(base_url, token, client) if client is not None else []

    return load


def _lease_serves(
    lease: FailoverLease | None,
    snapshot: FailoverSnapshot,
    *,
    holder: str | None = None,
    existing_request: bool = False,
) -> bool:
    return lease_current(lease, snapshot, holder=holder, existing_request=existing_request)


def _status_for(lease: FailoverLease | None, snapshot: FailoverSnapshot | None) -> FailoverStatus:
    if snapshot is None or lease is None or not _lease_serves(lease, snapshot):
        return project_tier(elected_host=None, reachable=frozenset()).model_copy(
            update={
                "snapshot_expires_at": snapshot.expires_at if snapshot else None,
            }
        )
    reachable = (
        frozenset({lease.holder, *(grant.voter for grant in lease.proof.grants)})
        if lease.proof
        else frozenset({lease.holder})
    )
    return project_tier(
        elected_host=lease.holder, reachable=reachable, snapshot_expires_at=snapshot.expires_at
    ).model_copy(
        update={
            "admission_available": True,
            "reason": "available",
            "authority_expires_at": min(snapshot.expires_at, lease_expiry(lease)),
        }
    )


def _refuse(reason: str, code: int = 503) -> JSONResponse:
    ingress_refusals_total.add(1, {"reason": reason})
    problem = FailoverProblem.model_validate({"status": code, "detail": reason, "reason": reason})
    return JSONResponse(
        status_code=code,
        content=problem.model_dump(mode="json"),
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
    )


def _resident_ids(
    snapshot: FailoverSnapshot, engines: list[FailoverResidentEngine]
) -> frozenset[UUID]:
    return frozenset(
        model.id
        for model in snapshot.models
        if any(resident_matches(model, engine) for engine in engines)
    )


def create_app(runtime: FailoverRuntime | None = None) -> FastAPI:
    runtime = runtime or FailoverRuntime()
    app = FastAPI(
        title="Coire Failover", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.runtime = runtime
    app.mount(
        "/assets",
        StaticFiles(directory=str(_STATIC_ROOT / "assets"), check_dir=False),
        name="assets",
    )

    def _snapshot() -> FailoverSnapshot:
        with tracer.start_as_current_span("coire.failover.snapshot"):
            try:
                snapshot = runtime.load_snapshot()
                if snapshot.membership.epoch != runtime.settings.failover_membership_epoch:
                    raise ValueError("snapshot epoch mismatch")
                if not snapshot.is_fresh(runtime.settings.failover_snapshot_max_age_s):
                    raise ValueError("snapshot expired")
            except (OSError, ValueError) as exc:
                snapshot_fresh.set(0)
                raise HTTPException(503, "snapshot_unavailable") from exc
            snapshot_fresh.set(1)
            return snapshot

    def authorised(snapshot: FailoverSnapshot, *, existing: bool = False) -> bool:
        return _lease_serves(
            runtime.load_lease(),
            snapshot,
            holder=runtime.settings.failover_member_name,
            existing_request=existing,
        )

    @app.middleware("http")
    async def route_identity(request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        implemented = path in {
            "/",
            "/ready",
            "/failover/tier",
            "/v1/models",
            "/v1/chat/completions",
        } or path.startswith("/assets/")
        refusal: Response | None = None
        request.state.lifecycle = False
        if implemented:
            assertion = request.headers.get("Cf-Access-Jwt-Assertion")
            monitor = runtime.settings.failover_monitor_token.get_secret_value()
            lifecycle = runtime.settings.failover_lifecycle_token.get_secret_value()
            if path == "/ready":
                if not monitor or not hmac.compare_digest(
                    monitor, request.headers.get("X-Coire-Failover-Monitor", "")
                ):
                    refusal = _refuse("identity_invalid", 401)
            elif (
                path == "/failover/tier"
                and lifecycle
                and hmac.compare_digest(
                    lifecycle, request.headers.get("X-Coire-Failover-Lifecycle", "")
                )
            ):
                request.state.lifecycle = True
            elif not assertion:
                refusal = _refuse("identity_invalid", 401)
            else:
                try:
                    request.state.claims = await _identity(runtime, assertion, _snapshot())
                except HTTPException as exc:
                    refusal = _refuse(str(exc.detail), exc.status_code)
        response = refusal if refusal is not None else await call_next(request)
        try:
            snapshot = runtime.load_snapshot()
        except (OSError, ValueError):
            snapshot = None
        projected = _status_for(runtime.load_lease() if snapshot else None, snapshot)
        response.headers["X-Coire-Tier"] = projected.tier.value
        response.headers["X-Coire-Persistence"] = "unavailable"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def degraded_home() -> HTMLResponse:
        try:
            html = (_STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(503, "frontend_unavailable") from None
        return HTMLResponse(html.replace("<html", '<html data-coire-tier="failover"', 1))

    @app.get("/ready", include_in_schema=False)
    async def ready() -> Response:
        try:
            snapshot = _snapshot()
        except HTTPException:
            return Response(status_code=503)
        return Response(status_code=200 if authorised(snapshot) else 503)

    @app.get(
        "/failover/tier",
        response_model=FailoverPublicStatus | FailoverStatus,
        description="Browser view excludes lifecycle counters; scoped lifecycle credentials select the extended view.",
    )
    async def tier(request: Request) -> FailoverPublicStatus | JSONResponse:
        try:
            snapshot = _snapshot()
        except HTTPException:
            snapshot = None
        status = _status_for(runtime.load_lease(), snapshot)
        if request.state.lifecycle:
            return JSONResponse(
                status.model_copy(update={"in_flight": runtime.in_flight}).model_dump(mode="json")
            )
        return FailoverPublicStatus.model_validate(status.model_dump(exclude={"in_flight"}))

    @app.get("/v1/models", response_model=GatewayModelList)
    async def models() -> GatewayModelList | JSONResponse:
        snapshot = _snapshot()
        if not authorised(snapshot):
            return _refuse("not_elected")
        local, peer = await asyncio.gather(runtime.local_resident(), runtime.peer_resident())
        if not authorised(_snapshot()):
            return _refuse("not_elected")
        resident = _resident_ids(snapshot, [*local, *peer])
        return GatewayModelList(
            data=[
                GatewayModel(
                    id=model.id,
                    created=int(snapshot.issued_at.timestamp()),
                    coire_load_state="loaded",
                    coire_description=model.display_name,
                    coire_context_window=model.context_window,
                )
                for model in snapshot.models
                if model.id in resident
            ]
        )

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> Response:
        with tracer.start_as_current_span("coire.failover.ingress"):
            snapshot = _snapshot()
            if not authorised(snapshot):
                return _refuse("not_elected")
            raw_bytes = bytearray()
            async for chunk in request.stream():
                raw_bytes.extend(chunk)
                if len(raw_bytes) > 65536:
                    return _refuse("request_limit_exceeded", 429)
            try:
                import json

                raw = json.loads(raw_bytes)
                if isinstance(raw, dict) and "@" in str(raw.get("model", "")):
                    return _refuse("adapter_unavailable")
                body = ChatCompletionRequest.model_validate(raw)
                if not isinstance(body.model, UUID) or body.coire_variant_id is not None:
                    return _refuse("exact_target_unavailable")
            except (ValidationError, ValueError):
                raise HTTPException(422, "invalid completion request") from None
            local, peer = await asyncio.gather(runtime.local_resident(), runtime.peer_resident())
            if not authorised(_snapshot()):
                return _refuse("not_elected")
            try:
                model, destination = choose_relay(
                    snapshot,
                    body.model,
                    local_resident_ids=_resident_ids(snapshot, local),
                    peer_resident_ids=_resident_ids(snapshot, peer),
                )
            except FailoverModelUnavailable:
                return _refuse("model_not_resident")
            claims = request.state.claims
            subject = str(claims["sub"])
            eligible = next(
                item
                for item in snapshot.subjects
                if item.subject == subject and item.issuer == claims["iss"]
            )
            try:
                body = bounded_text_request(body, model, eligible.limits)
                runtime.counters.acquire(subject, effective_limits(eligible.limits, model))
            except FailoverLimitError as exc:
                return _refuse(exc.reason, 429 if exc.reason == "request_limit_exceeded" else 503)
            lease = runtime.load_lease()
            if lease is None or runtime.relay is None:
                runtime.counters.release(subject)
                return _refuse("not_elected")
            engines = local if destination == "local" else peer
            engine = next(item for item in engines if resident_matches(model, item))
            expiry = datetime.fromtimestamp(float(claims["exp"]), UTC)
            try:
                authority = await runtime.issue_authority(
                    FailoverRelayIssuance(
                        engine_id=engine.engine_id,
                        request=body,
                        access_assertion=request.headers["Cf-Access-Jwt-Assertion"],
                    )
                )
                if (
                    not authorised(_snapshot())
                    or not relay_is_current(authority, snapshot, body)
                    or authority.holder != lease.holder
                    or authority.engine_id != engine.engine_id
                    or authority.identity_subject != subject
                    or authority.expires_at > expiry
                ):
                    raise ValueError("not_elected")
            except (ValueError, httpx.HTTPError):
                runtime.counters.release(subject)
                return _refuse("not_elected")
            except BaseException:
                runtime.counters.release(subject)
                raise
            relay_request = FailoverRelayRequest(
                engine_id=engine.engine_id,
                model_slug=model.slug,
                request=body,
                authority=authority,
                access_assertion=request.headers["Cf-Access-Jwt-Assertion"],
            )
            runtime.in_flight += 1

            def still_authorised() -> bool:
                if datetime.now(UTC) >= min(expiry, authority.expires_at):
                    return False
                try:
                    current = _snapshot()
                except HTTPException:
                    return False
                return authorised(current, existing=True) and any(
                    item.subject == subject and item.issuer == claims["iss"] and item.eligible
                    for item in current.subjects
                )

            def release() -> None:
                runtime.in_flight -= 1
                runtime.counters.release(subject)

            try:
                response = await guarded_await(
                    runtime.relay(relay_request, destination), still_authorised
                )
            except FailoverAuthorityLost:
                release()
                return _refuse("not_elected")
            except BaseException:
                release()
                raise
            if not isinstance(response, StreamingResponse):
                release()
                return response if still_authorised() else _refuse("not_elected")
            source = aiter(response.body_iterator)

            async def guarded_stream() -> AsyncIterator[bytes | str | memoryview]:
                pending: asyncio.Task[bytes | str | memoryview] | None = None
                try:
                    while still_authorised():
                        if pending is None:

                            async def next_chunk() -> bytes | str | memoryview:
                                return await anext(source)

                            pending = asyncio.create_task(next_chunk())
                        done, _ = await asyncio.wait({pending}, timeout=0.5)
                        if not done:
                            continue
                        try:
                            chunk = pending.result()
                        except StopAsyncIteration:
                            return
                        pending = None
                        if not still_authorised():
                            break
                        yield chunk
                    yield b'event: error\ndata: {"reason":"not_elected","coire_interrupted":true}\n\n'
                finally:
                    if pending is not None:
                        pending.cancel()
                        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                            await pending
                    close = getattr(source, "aclose", None)
                    if close is not None:
                        await close()
                    release()

            response.body_iterator = guarded_stream()
            return response

    return app


async def _identity(
    runtime: FailoverRuntime, assertion: str | None, snapshot: FailoverSnapshot
) -> dict[str, Any]:
    try:
        return await verify_access_assertion(assertion, snapshot, client=runtime.access_client)
    except AccessIdentityError as exc:
        ingress_refusals_total.add(1, {"reason": "identity_invalid"})
        raise HTTPException(401, "identity_invalid") from exc


app = create_app()
