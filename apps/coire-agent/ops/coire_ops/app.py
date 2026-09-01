"""Long-lived HTTP boundary for the isolated core-only ops service."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException, status

from coire_core.models.health import HealthResponse, HealthStatus, ReadyResponse
from coire_core.models.ops import OpsServiceTurnRequest, OpsTurnResponse
from coire_core.settings import get_settings
from coire_ops.admin_client import AdminClient
from coire_ops.model import OpsModel
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
            model_id=settings.ops_model_id,
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

    @app.post("/turn", response_model=OpsTurnResponse)
    async def turn(
        body: OpsServiceTurnRequest,
        authorization: str = Header(default=""),
    ) -> OpsTurnResponse:
        expected = app.state.expected_token
        if expected is None:
            expected = get_settings().ops_service_token.get_secret_value()
        presented = authorization.removeprefix("Bearer ")
        if not expected or not hmac.compare_digest(expected, presented):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "ops service credential required")
        service: OpsService = app.state.ops_service
        try:
            return await service.turn(
                conversation_id=body.conversation_id,
                question=body.question,
            )
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    return app
