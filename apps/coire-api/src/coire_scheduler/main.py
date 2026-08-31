"""Durable acquisition and placement scheduler.

Feature 000 ships only `/ready`. The placement scheduler, memory ledger and auto-unload are
feature 004; durable job workflows arrive with feature 002. Separating it from request handling
now means a future scheduler restart never interrupts a streaming response.

This is the only service attached to `coire-docker`, and it reaches the Docker socket solely
through the allowlisted proxy (FR-007) — never directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from coire_api.telemetry import configure_telemetry
from coire_core.models.health import ReadyResponse
from coire_core.settings import get_settings
from coire_scheduler.dbos_runtime import DBOSRuntime

SERVICE_NAME = "coire-scheduler"
__version__ = "0.1.0"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_telemetry(SERVICE_NAME, settings.service_version, settings.otlp_endpoint)
    runtime = DBOSRuntime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime.launch()
        app.state.dbos = runtime
        try:
            yield
        finally:
            runtime.destroy()

    app = FastAPI(
        title="Coire scheduler",
        version=__version__,
        docs_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/ready", response_model=ReadyResponse)
    async def get_ready() -> ReadyResponse:
        if not runtime.launched:
            from fastapi import HTTPException

            raise HTTPException(503, "durable workflow runtime is not ready")
        return ReadyResponse(service=SERVICE_NAME, version=__version__)

    return app


def main() -> None:
    # container-internal only; nothing is published from this service
    uvicorn.run(create_app(), host="0.0.0.0", port=8002, access_log=False)


if __name__ == "__main__":
    main()
