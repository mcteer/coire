"""Long-lived HTTP boundary for the isolated core-only ops service."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException, status

from coire_core.models.health import HealthResponse, HealthStatus, ReadyResponse
from coire_core.models.ops import OpsServiceTurnRequest, OpsTurnResponse
from coire_core.models.upgrades import UpgradeInventoryEntry, UpgradeOwnerInventory
from coire_core.settings import get_settings
from coire_ops.admin_client import AdminClient
from coire_ops.model import OPS_ANTHROPIC_MODEL, OpsModel
from coire_ops.service import OpsService


def _default_service() -> OpsService:
    settings = get_settings()
    token = settings.ops_service_token.get_secret_value()
    return OpsService(
        admin=AdminClient(
            api_url=settings.ops_api_url,
            token=token,
            timeout_s=settings.ops_request_timeout_s,
        ),
        model=OpsModel(
            gateway_url=settings.ops_gateway_url,
            token=token,
            model_id=(
                OPS_ANTHROPIC_MODEL
                if settings.ops_model_source == "anthropic"
                else settings.ops_model_id
            ),
            model_source=settings.ops_model_source,
            ops_api_url=settings.ops_api_url,
            timeout_s=settings.ops_request_timeout_s,
        ),
        service_instance=settings.ops_service_instance,
        heartbeat_s=settings.ops_session_heartbeat_s,
    )


def create_app(
    service_factory: Callable[[], OpsService] = _default_service,
    *,
    expected_token: str | None = None,
) -> FastAPI:
    """Create the narrow ops service; mutation authority remains in coire-api."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = service_factory()
        app.state.ops_service = service
        await service.start()
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await service.stop()

    app = FastAPI(
        title="Coire Ops",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.ready = False
    app.state.expected_token = expected_token
    app.state.active_turns = 0

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        service: OpsService | None = getattr(app.state, "ops_service", None)
        healthy = service is not None and service.model_healthy
        return HealthResponse(
            status=HealthStatus.HEALTHY if healthy else HealthStatus.DEGRADED,
            version="0.1.0",
            generated_at=datetime.now(UTC),
        )

    @app.get("/ready", response_model=ReadyResponse)
    async def ready() -> ReadyResponse:
        # FastAPI does not serve requests before lifespan startup, so a handled request is ready.
        return ReadyResponse(service="coire-ops", version="0.1.0", ready=app.state.ready)

    def check_credential(authorization: str) -> None:
        expected = app.state.expected_token
        if expected is None:
            expected = get_settings().ops_service_token.get_secret_value()
        if not expected or not hmac.compare_digest(expected, authorization.removeprefix("Bearer ")):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "ops service credential required")

    @app.get("/internal/upgrades/inventory", response_model=UpgradeOwnerInventory)
    async def upgrade_inventory(authorization: str = Header(default="")) -> UpgradeOwnerInventory:
        check_credential(authorization)
        return UpgradeOwnerInventory(
            owner="ops",
            status="complete",
            entries=()
            if not app.state.active_turns
            else (
                UpgradeInventoryEntry(
                    owner="ops", kind="core-requests", work_id="inflight", state="active"
                ),
            ),
        )

    @app.post("/turn", response_model=OpsTurnResponse)
    async def turn(
        body: OpsServiceTurnRequest,
        authorization: str = Header(default=""),
    ) -> OpsTurnResponse:
        check_credential(authorization)
        service: OpsService = app.state.ops_service
        app.state.active_turns += 1
        try:
            if get_settings().upgrades_enabled:
                try:
                    allowed = await service.upgrade_admission()
                except Exception:
                    raise HTTPException(
                        503, "ops maintenance authority unavailable", headers={"Retry-After": "5"}
                    ) from None
                if not allowed:
                    raise HTTPException(
                        503, "ops is draining for an upgrade", headers={"Retry-After": "5"}
                    )
            try:
                return await service.turn(
                    conversation_id=body.conversation_id, question=body.question
                )
            except RuntimeError as exc:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        finally:
            app.state.active_turns -= 1

    return app
