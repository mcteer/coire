"""MCP server stub.

Feature 000 ships only `/ready`, which is enough to prove independent restart (spec US2). The
three coding tools — research, plan, apply — are feature 013. This is a separate image and a
separate container so it can be stopped or upgraded without dropping chat traffic.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from coire_api.telemetry import configure_telemetry
from coire_core.models.health import ReadyResponse
from coire_core.settings import get_settings

SERVICE_NAME = "coire-mcp"
__version__ = "0.1.0"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_telemetry(SERVICE_NAME, settings.service_version, settings.otlp_endpoint)
    app = FastAPI(title="Coire MCP", version=__version__, docs_url=None, openapi_url=None)

    @app.get("/ready", response_model=ReadyResponse)
    async def get_ready() -> ReadyResponse:
        return ReadyResponse(service=SERVICE_NAME, version=__version__)

    return app


def main() -> None:
    # container-internal only; nothing is published from this service
    uvicorn.run(create_app(), host="0.0.0.0", port=8001, access_log=False)


if __name__ == "__main__":
    main()
