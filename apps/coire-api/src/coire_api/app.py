"""FastAPI application factory for the control plane."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from coire_api import __version__
from coire_api.db import dispose_engine, init_engine, session_scope
from coire_api.routes import (
    admin_acquisitions,
    admin_console,
    admin_evaluations,
    admin_identity,
    admin_ledger,
    admin_models,
    admin_nodes,
    admin_runs,
    admin_sharding,
    admin_variants,
    health,
    instances,
    me,
    models,
    nodes,
    runs,
    v1,
)
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
        from coire_api.identity.bootstrap import ensure_bootstrap_admin

        async with session_scope() as session:
            await ensure_bootstrap_admin(session, settings)
        from coire_api.gateway.proxy import close_engine_client, init_engine_client

        init_engine_client()
        from coire_api.link_probe_coordinator import LinkProbeCoordinator

        link_probe_coordinator = LinkProbeCoordinator(settings)
        await link_probe_coordinator.start()
        app.state.link_probe_coordinator = link_probe_coordinator
        from coire_api.nodes_prober import NodeProber

        prober = NodeProber(settings)
        prober.set_link_probe_coordinator(link_probe_coordinator)
        await prober.start()
        app.state.prober = prober

        from coire_api.registry.reconciler import RegistryReconciler

        reconciler = RegistryReconciler(settings)
        await reconciler.start()
        app.state.reconciler = reconciler
        prober.set_reconciler(reconciler)
        from coire_api.registry.acquisition_executor import AcquisitionCommandExecutor

        acquisition_executor = AcquisitionCommandExecutor(settings)
        await acquisition_executor.start()
        app.state.acquisition_executor = acquisition_executor
        from coire_api.placement.executor import PlacementCommandExecutor

        placement_executor = PlacementCommandExecutor(settings)
        await placement_executor.start()
        app.state.placement_executor = placement_executor
        from coire_api.run_executor import RunCommandExecutor

        run_executor = RunCommandExecutor(settings)
        await run_executor.start()
        app.state.run_executor = run_executor
        from coire_api.run_reconciler import RunReconciliationCoordinator

        run_reconciler = RunReconciliationCoordinator(settings)
        await run_reconciler.start()
        app.state.run_reconciler = run_reconciler
        from coire_api.shard_executor import ShardCommandExecutor

        shard_executor = ShardCommandExecutor(settings)
        await shard_executor.start()
        app.state.shard_executor = shard_executor
        from coire_api.shard_reconciler import ShardReconciler

        shard_reconciler = ShardReconciler(settings)
        await shard_reconciler.start()
        app.state.shard_reconciler = shard_reconciler
        from coire_api.benchmark_executor import BenchmarkCommandExecutor

        benchmark_executor = BenchmarkCommandExecutor(settings)
        await benchmark_executor.start()
        app.state.benchmark_executor = benchmark_executor
        logger.info("coire-api %s started", __version__)
        try:
            yield
        finally:
            await benchmark_executor.stop()
            await shard_reconciler.stop()
            await shard_executor.stop()
            await run_reconciler.stop()
            await run_executor.stop()
            await placement_executor.stop()
            await acquisition_executor.stop()
            await reconciler.stop()
            await prober.stop()
            await link_probe_coordinator.stop()
            await close_engine_client()
            await dispose_engine()

    app = FastAPI(
        title="Coire control plane",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    from coire_api.identity.access import AccessVerifier

    app.state.access_verifier = AccessVerifier(settings)
    app.include_router(health.router)
    app.include_router(instances.router)
    app.include_router(nodes.router)
    app.include_router(models.router)
    app.include_router(runs.router)
    app.include_router(me.router)
    app.include_router(admin_acquisitions.router)
    app.include_router(admin_console.router)
    app.include_router(admin_evaluations.router)
    app.include_router(admin_ledger.router)
    app.include_router(admin_identity.router)
    app.include_router(admin_variants.router)
    app.include_router(admin_models.router)
    app.include_router(admin_nodes.router)
    app.include_router(admin_runs.router)
    app.include_router(admin_sharding.router)
    app.include_router(v1.router)

    @app.middleware("http")
    async def authenticate_application_request(request: Request, call_next):  # type: ignore[no-untyped-def]
        from coire_api.auth import (
            ANONYMOUS,
            audit_authentication_failure,
            authenticate_request,
            bind_principal,
            reset_principal,
        )
        from coire_api.identity.limits import MonthlyQuotaExceeded, RateLimitExceeded

        anonymous_paths = {"/ready", "/health"}
        separately_authenticated_paths = {"/api/v1/nodes/register"}
        try:
            principal = (
                ANONYMOUS
                if request.url.path in anonymous_paths | separately_authenticated_paths
                else await authenticate_request(request)
            )
        except RateLimitExceeded as exc:
            retry_seconds = max(1, int((exc.retry_at - datetime.now(UTC)).total_seconds()))
            return JSONResponse(
                status_code=429,
                media_type="application/problem+json",
                headers={"Retry-After": str(retry_seconds)},
                content={
                    "type": "urn:coire:problem:rate-limit-exceeded",
                    "title": "API key rate limit exceeded",
                    "status": 429,
                    "detail": "request rate limit exceeded",
                    "code": "rate_limit_exceeded",
                    "retry_at": exc.retry_at.isoformat(),
                },
            )
        except MonthlyQuotaExceeded as exc:
            return JSONResponse(
                status_code=429,
                media_type="application/problem+json",
                content={
                    "type": "urn:coire:problem:monthly-quota-exceeded",
                    "title": "API key monthly quota exceeded",
                    "status": 429,
                    "detail": "monthly token budget exhausted",
                    "code": "monthly_quota_exceeded",
                    "budget_tokens": exc.budget_tokens,
                    "consumed_tokens": exc.consumed_tokens,
                    "retry_at": exc.resets_at.isoformat(),
                },
            )
        if (
            request.url.path not in anonymous_paths | separately_authenticated_paths
            and principal is ANONYMOUS
        ):
            await audit_authentication_failure(request, reason="credential_invalid")
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={"detail": "valid identity or API key required"},
            )
        token = bind_principal(principal)
        try:
            return await call_next(request)
        finally:
            reset_principal(token)

    @app.middleware("http")
    async def trace_compatible_request(request: Request, call_next):  # type: ignore[no-untyped-def]
        run_problem = request.url.path.startswith(("/api/v1/runs", "/api/v1/admin/runs"))
        if not request.url.path.startswith("/v1/") and not run_problem:
            return await call_next(request)
        from coire_api.gateway.telemetry import tracer

        with tracer.start_as_current_span("coire.gateway.request") as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("url.path", request.url.path)
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
            return response

    @app.middleware("http")
    async def attach_request_identity(request: Request, call_next):  # type: ignore[no-untyped-def]
        from coire_api.auth import bind_request_id, reset_request_id

        raw = request.headers.get("x-request-id", "")
        try:
            request_id = uuid.UUID(raw) if raw else uuid.uuid4()
        except ValueError:
            request_id = uuid.uuid4()
        token = bind_request_id(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = str(request_id)
            return response
        finally:
            reset_request_id(token)

    @app.exception_handler(HTTPException)
    async def compatible_problem(request: Request, exc: HTTPException) -> JSONResponse:
        """Keep legacy control routes stable while `/v1` uses RFC 9457."""
        run_problem = request.url.path.startswith(("/api/v1/runs", "/api/v1/admin/runs"))
        if not request.url.path.startswith("/v1/") and not run_problem:
            return JSONResponse(
                status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
            )
        raw_detail: object = exc.detail
        detail = (
            str(raw_detail.get("detail", "request failed"))
            if isinstance(raw_detail, dict)
            else str(raw_detail)
        )
        code = raw_detail.get("code") if isinstance(raw_detail, dict) else None
        return JSONResponse(
            status_code=exc.status_code,
            media_type="application/problem+json",
            headers=exc.headers,
            content={
                "type": f"urn:coire:problem:{code}" if code else "about:blank",
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
            from coire_api.auth import bound_principal
            from coire_api.gateway.usage import UsageTracker
            from coire_core.models.gateway import GatewayProtocol, UsageOutcome

            principal = bound_principal()
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
