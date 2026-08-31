"""FastAPI application factory for the control plane."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
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

    @app.middleware("http")
    async def trace_compatible_request(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)
        from coire_api.gateway.telemetry import tracer

        with tracer.start_as_current_span("coire.gateway.request") as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("url.path", request.url.path)
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
            return response

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

    @app.exception_handler(RequestValidationError)
    async def account_compatible_validation_failure(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Account malformed inference requests before returning FastAPI's normal 422."""
        protocol = {
            "/v1/chat/completions": "openai",
            "/v1/messages": "anthropic",
        }.get(request.url.path)
        if protocol is not None:
            from coire_api.auth import principal_from_bearer
            from coire_api.gateway.usage import UsageTracker
            from coire_core.models.gateway import GatewayProtocol, UsageOutcome

            header = request.headers.get("authorization", "")
            scheme, _, token = header.partition(" ")
            credentials = (
                HTTPAuthorizationCredentials(scheme=scheme, credentials=token)
                if scheme.lower() == "bearer" and token
                else None
            )
            if credentials is None and request.headers.get("x-api-key"):
                credentials = HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials=request.headers["x-api-key"]
                )
            principal = principal_from_bearer(
                credentials, expected=settings.admin_token.get_secret_value()
            )
            requested_model = "<invalid>"
            try:
                raw = await request.body()
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    requested_model = str(parsed.get("model", "<missing>"))[:255]
            except (ValueError, TypeError):
                pass
            tracker = UsageTracker(principal, requested_model, GatewayProtocol(protocol))
            await tracker.finish(UsageOutcome.REFUSED, failure_code="request_validation")
        response = await request_validation_exception_handler(request, exc)
        return JSONResponse(
            status_code=response.status_code,
            content=jsonable_encoder({"detail": exc.errors()}),
            headers=dict(response.headers),
        )

    FastAPIInstrumentor.instrument_app(app)
    return app
