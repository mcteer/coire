"""Fail-closed ASGI surface for a promoted Studio."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from coire_core.failover_crypto import verify_ed25519
from coire_core.failover_snapshot import SnapshotError, load_verified_snapshot
from coire_core.failover_telemetry import ingress_refusals_total, snapshot_fresh, tracer
from coire_core.models.failover import (
    FailoverLease,
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
_STATIC_ROOT = Path("/app/static")


class FailoverRuntime:
    """Injected edges so contract tests never touch a node, a database, or the network."""

    def __init__(
        self,
        *,
        load_snapshot: SnapshotLoader | None = None,
        load_lease: LeaseLoader | None = None,
        local_resident: ResidentLoader | None = None,
        peer_resident: ResidentLoader | None = None,
        relay: CompletionRelay | None = None,
        access_client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.load_snapshot = load_snapshot or _file_snapshot(self.settings)
        self.load_lease = load_lease or _file_lease(self.settings)
        token = relay_token(self.settings)
        self._client = httpx.AsyncClient() if token and relay is None else None
        local_base = self.settings.failover_local_relay_url.strip()
        peer_base = self.settings.failover_peer_relay_url.strip()
        self.local_resident = local_resident or _resident_loader(local_base, token, self._client)
        self.peer_resident = peer_resident or _resident_loader(peer_base, token, self._client)
        self.relay = relay or (
            completion_relay(self.settings, self._client) if self._client is not None else None
        )
        self.access_client = access_client
        self.in_flight = 0


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
        path = Path(settings.failover_proof_path)
        try:
            return FailoverLease.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    return load


def _resident_loader(base_url: str, token: str, client: httpx.AsyncClient | None) -> ResidentLoader:
    async def load() -> list[FailoverResidentEngine]:
        if client is None:
            return []
        return await fetch_resident(base_url, token, client)

    return load


def _lease_serves(lease: FailoverLease | None, snapshot: FailoverSnapshot) -> bool:
    if lease is None:
        return False
    now = datetime.now(UTC)
    if (
        lease.proof is not None
        and lease.proof.is_current(now)
        and lease.proof.is_valid_for(snapshot.membership, now)
    ):
        return True
    override = lease.break_glass
    if override is not None and override.is_current(now):
        core = next(member for member in snapshot.membership.members if member.name == "coire-core")
        return verify_ed25519(override.canonical_bytes(), override.signature, core.public_key)
    return False


def _status_for(lease: FailoverLease | None, snapshot: FailoverSnapshot | None) -> FailoverStatus:
    if snapshot is None or lease is None or not _lease_serves(lease, snapshot):
        return project_tier(elected_host=None, reachable=frozenset())
    if lease.proof is not None:
        reachable = frozenset({lease.holder, *(grant.voter for grant in lease.proof.grants)})
    else:
        reachable = frozenset({lease.holder})
    return project_tier(
        elected_host=lease.holder,
        reachable=reachable,
        snapshot_expires_at=snapshot.expires_at,
    )


def _refuse(reason: str, code: int = status.HTTP_503_SERVICE_UNAVAILABLE) -> JSONResponse:
    ingress_refusals_total.add(1, {"reason": reason})
    return JSONResponse(status_code=code, content={"detail": reason, "reason": reason})


def _resident_ids(
    snapshot: FailoverSnapshot, engines: list[FailoverResidentEngine]
) -> frozenset[UUID]:
    by_slug = {model.slug: model.id for model in snapshot.models}
    return frozenset(by_slug[engine.slug] for engine in engines if engine.slug in by_slug)


def create_app(runtime: FailoverRuntime | None = None) -> FastAPI:
    """Create the deliberately tiny, non-authoritative failover surface."""
    runtime = runtime or FailoverRuntime()
    app = FastAPI(
        title="Coire Failover", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.runtime = runtime
    app.mount(
        "/assets",
        StaticFiles(directory=str(_STATIC_ROOT / "assets"), check_dir=False),
        name="assets",
    )

    @app.get("/", include_in_schema=False)
    async def degraded_home() -> HTMLResponse:
        try:
            html = (_STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "frontend_unavailable"
            ) from None
        return HTMLResponse(html.replace("<html", '<html data-coire-tier="failover"', 1))

    def _snapshot() -> FailoverSnapshot:
        with tracer.start_as_current_span("coire.failover.snapshot"):
            try:
                snapshot = runtime.load_snapshot()
            except (OSError, ValueError) as exc:
                snapshot_fresh.set(0)
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, "snapshot_unavailable"
                ) from exc
            if not snapshot.is_fresh(runtime.settings.failover_snapshot_max_age_s):
                snapshot_fresh.set(0)
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "snapshot_unavailable")
            snapshot_fresh.set(1)
            return snapshot

    @app.middleware("http")
    async def tier_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        snapshot: FailoverSnapshot | None
        try:
            snapshot = runtime.load_snapshot()
        except (OSError, ValueError):
            snapshot = None
        lease = runtime.load_lease() if snapshot is not None else None
        projected = _status_for(lease, snapshot)
        response.headers["X-Coire-Tier"] = projected.tier.value
        response.headers["X-Coire-Persistence"] = "unavailable"
        return response

    @app.get("/ready", include_in_schema=False)
    async def ready() -> Response:
        try:
            snapshot = _snapshot()
        except HTTPException:
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        if not _lease_serves(runtime.load_lease(), snapshot):
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(status_code=status.HTTP_200_OK)

    @app.get("/failover/tier", response_model=FailoverStatus)
    async def tier() -> FailoverStatus:
        try:
            snapshot = _snapshot()
        except HTTPException:
            return project_tier(elected_host=None, reachable=frozenset()).model_copy(
                update={"in_flight": runtime.in_flight}
            )
        return _status_for(runtime.load_lease(), snapshot).model_copy(
            update={"in_flight": runtime.in_flight}
        )

    @app.get("/v1/models", response_model=GatewayModelList)
    async def models(
        cf_access_jwt_assertion: str | None = Header(default=None),
    ) -> GatewayModelList | JSONResponse:
        with tracer.start_as_current_span("coire.failover.ingress") as span:
            span.set_attribute("coire.failover.route", "/v1/models")
            snapshot = _snapshot()
            await _identity(runtime, cf_access_jwt_assertion, snapshot)
            if not _lease_serves(runtime.load_lease(), snapshot):
                return _refuse("not_elected")
            local = await runtime.local_resident()
            peer = await runtime.peer_resident()
            resident = {engine.slug for engine in (*local, *peer)}
            data = [
                GatewayModel(
                    id=model.id,
                    created=int(snapshot.issued_at.timestamp()),
                    coire_load_state="loaded",
                    coire_description=model.display_name,
                    coire_context_window=model.context_window,
                )
                for model in snapshot.models
                if not model.entitlement and model.slug in resident
            ]
            return GatewayModelList(data=data)

    @app.post("/v1/chat/completions")
    async def completions(
        request: Request,
        cf_access_jwt_assertion: str | None = Header(default=None),
    ) -> Response:
        with tracer.start_as_current_span("coire.failover.ingress") as span:
            span.set_attribute("coire.failover.route", "/v1/chat/completions")
            snapshot = _snapshot()
            await _identity(runtime, cf_access_jwt_assertion, snapshot)
            if not _lease_serves(runtime.load_lease(), snapshot):
                return _refuse("not_elected")
            try:
                raw = await request.json()
                if isinstance(raw, dict) and "@" in str(raw.get("model", "")):
                    return _refuse("adapter_unavailable")
                body = ChatCompletionRequest.model_validate(raw)
                if not isinstance(body.model, UUID) or body.coire_variant_id is not None:
                    return _refuse("exact_target_unavailable")
            except (ValidationError, ValueError) as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid completion request"
                ) from exc
            local = await runtime.local_resident()
            peer = await runtime.peer_resident()
            try:
                model, destination = choose_relay(
                    snapshot,
                    body.model,
                    local_resident_ids=_resident_ids(snapshot, local),
                    peer_resident_ids=_resident_ids(snapshot, peer),
                )
            except FailoverModelUnavailable:
                return _refuse("model_not_resident")
            engines = local if destination == "local" else peer
            engine = next(item for item in engines if item.slug == model.slug)
            relay_request = FailoverRelayRequest(
                engine_id=engine.engine_id, model_slug=model.slug, request=body
            )
            if runtime.relay is None:
                return _refuse("not_elected")
            runtime.in_flight += 1
            try:
                response = await runtime.relay(relay_request, destination)
            except BaseException:
                runtime.in_flight -= 1
                raise
            if isinstance(response, StreamingResponse):
                source = response.body_iterator

                async def counted_stream() -> AsyncIterator[bytes | str | memoryview]:
                    try:
                        async for chunk in source:
                            yield chunk
                    finally:
                        runtime.in_flight -= 1

                response.body_iterator = counted_stream()
            else:
                runtime.in_flight -= 1
            return response

    return app


async def _identity(
    runtime: FailoverRuntime, assertion: str | None, snapshot: FailoverSnapshot
) -> None:
    try:
        await verify_access_assertion(assertion, snapshot, client=runtime.access_client)
    except AccessIdentityError as exc:
        ingress_refusals_total.add(1, {"reason": "identity_invalid"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "identity_invalid") from exc


app = create_app()
