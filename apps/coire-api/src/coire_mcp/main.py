"""MCP server stub.

Feature 000 ships only `/ready`, which is enough to prove independent restart (spec US2). The
three coding tools — research, plan, apply — are feature 013. This is a separate image and a
separate container so it can be stopped or upgraded without dropping chat traffic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from coire_api.auth import (
    ANONYMOUS,
    PrincipalKind,
    audit_authentication_failure,
    authenticate_request,
    bind_principal,
    reset_principal,
)
from coire_api.db import dispose_engine, init_engine
from coire_api.identity.access import AccessVerifier
from coire_api.identity.limits import MonthlyQuotaExceeded, RateLimitExceeded
from coire_api.telemetry import configure_telemetry
from coire_core.models.health import ReadyResponse
from coire_core.settings import get_settings

SERVICE_NAME = "coire-mcp"
__version__ = "0.1.0"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_telemetry(SERVICE_NAME, settings.service_version, settings.otlp_endpoint)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        try:
            yield
        finally:
            await dispose_engine()

    app = FastAPI(
        title="Coire MCP",
        version=__version__,
        docs_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.access_verifier = AccessVerifier(settings)

    @app.middleware("http")
    async def authenticate_mcp(request: Request, call_next):  # type: ignore[no-untyped-def]
        try:
            principal = (
                ANONYMOUS if request.url.path == "/ready" else await authenticate_request(request)
            )
        except RateLimitExceeded as exc:
            return JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": str(
                        max(1, int((exc.retry_at - datetime.now(UTC)).total_seconds()))
                    )
                },
                content={
                    "detail": "request rate limit exceeded",
                    "code": "rate_limit_exceeded",
                    "retry_at": exc.retry_at.isoformat(),
                },
            )
        except MonthlyQuotaExceeded as exc:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "monthly token budget exhausted",
                    "code": "monthly_quota_exceeded",
                    "budget_tokens": exc.budget_tokens,
                    "consumed_tokens": exc.consumed_tokens,
                    "retry_at": exc.resets_at.isoformat(),
                },
            )
        if request.url.path != "/ready" and principal is ANONYMOUS:
            await audit_authentication_failure(request, reason="credential_invalid")
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={"detail": "valid identity or API key required"},
            )
        if (
            request.url.path != "/ready"
            and principal.kind is PrincipalKind.API_KEY
            and "mcp" not in principal.scopes
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "API key requires mcp scope"},
            )
        token = bind_principal(principal)
        try:
            return await call_next(request)
        finally:
            reset_principal(token)

    @app.get("/ready", response_model=ReadyResponse)
    async def get_ready() -> ReadyResponse:
        return ReadyResponse(service=SERVICE_NAME, version=__version__)

    return app


def main() -> None:
    # container-internal only; nothing is published from this service
    uvicorn.run(create_app(), host="0.0.0.0", port=8001, access_log=False)


if __name__ == "__main__":
    main()
