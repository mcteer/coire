"""Long-lived HTTP boundary for the isolated core-only ops service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

from coire_core.models.health import HealthResponse, HealthStatus, ReadyResponse


def create_app() -> FastAPI:
    """Create the narrow ops service; mutation authority remains in coire-api."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False

    app = FastAPI(
        title="Coire Ops",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.ready = False

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status=HealthStatus.HEALTHY,
            version="0.1.0",
            generated_at=datetime.now(UTC),
        )

    @app.get("/ready", response_model=ReadyResponse)
    async def ready() -> ReadyResponse:
        # FastAPI does not serve requests before lifespan startup, so a handled request is ready.
        return ReadyResponse(service="coire-ops", version="0.1.0", ready=True)

    return app
