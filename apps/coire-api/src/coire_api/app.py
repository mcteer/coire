"""FastAPI application factory for the control plane."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from coire_api import __version__
from coire_api.db import dispose_engine, init_engine
from coire_api.routes import admin_models, admin_nodes, health, models, nodes, v1
from coire_api.telemetry import configure_telemetry
from coire_core.settings import Settings, get_settings

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the control-plane app.

    The node prober is started in the lifespan (feature 000's only background task). It is
    started last and stopped first so a slow probe never delays readiness.
    """
    settings = settings or get_settings()
    configure_telemetry("coire-api", settings.service_version, settings.otlp_endpoint)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        from coire_api.gateway.proxy import close_engine_client, init_engine_client

        init_engine_client()
        from coire_api.nodes_prober import NodeProber

        prober = NodeProber(settings)
        await prober.start()
        app.state.prober = prober

        from coire_api.registry.reconciler import RegistryReconciler

        reconciler = RegistryReconciler(settings)
        await reconciler.start()
        app.state.reconciler = reconciler
        prober.set_reconciler(reconciler)
        logger.info("coire-api %s started", __version__)
        try:
            yield
        finally:
            await reconciler.stop()
            await prober.stop()
            await close_engine_client()
            await dispose_engine()

    app = FastAPI(
        title="Coire control plane",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(health.router)
    app.include_router(nodes.router)
    app.include_router(models.router)
    app.include_router(admin_models.router)
    app.include_router(admin_nodes.router)
    app.include_router(v1.router)

    @app.exception_handler(HTTPException)
    async def compatible_problem(request: Request, exc: HTTPException) -> JSONResponse:
        """Keep legacy control routes stable while `/v1` uses RFC 9457."""
        if not request.url.path.startswith("/v1/"):
            return JSONResponse(
                status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
            )
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(
            status_code=exc.status_code,
            media_type="application/problem+json",
            headers=exc.headers,
            content={
                "type": "about:blank",
                "title": detail,
                "status": exc.status_code,
                "detail": detail,
                "instance": request.url.path,
            },
        )

    FastAPIInstrumentor.instrument_app(app)
    return app
