"""Scoped compatibility API in the ordinary hardened API image.

No ordinary API workers, user harness or engine runs here. The frozen incumbent
stays untouched; this instance performs the separately audited recovery protocol.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from coire_api.app import create_app
from coire_api.db import dispose_engine, init_engine
from coire_api.upgrades.recovery_support_scope import scope
from coire_core.settings import Settings

ADMIN_PATHS = frozenset(
    {"/api/v1/admin/upgrades/recovery-schema", "/api/v1/admin/upgrades/recoveries"}
)
NODE_SUFFIXES = frozenset(
    {
        "recovery-initial-observation",
        "recovery-cold-continuation-context",
        "recovery-cold-continuation-observation",
        "recovery-cold-continuation-binding",
        "recovery-unstarted-context",
        "recovery-unstarted-observation",
        "recovery-support-context",
        "recovery-cold-claim",
        "recovery-cold-check",
        "recovery-cold-state",
        "recovery-cold-smoke",
        "recovery-cold-verification",
        "recovery-cold-verification-check",
        "recovery-cold-handback",
        "recovery-cold-handback-check",
        "recovery-cold-handback-receipt",
        "recovery-cold-resolve",
        "recovery-cold-resolution-state",
        "peer-inventory",
        "unclaimed-cancellation",
        "native-terminal",
    }
)


def create_recovery_support(settings: Settings) -> FastAPI:
    job, generation, source = (
        settings.upgrade_recovery_support_job_id,
        settings.upgrade_recovery_support_generation,
        settings.upgrade_recovery_support_source_sha256,
    )
    if job is None or generation is None or source is None or not settings.upgrades_enabled:
        raise ValueError("Compatibility API has no explicit original recovery scope")
    app = create_app(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        try:
            yield
        finally:
            await dispose_engine()

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def restricted_surface(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        parts = path.split("/")
        allowed = (
            path == "/ready"
            or path in ADMIN_PATHS
            or (
                len(parts) == 7
                and parts[:5] == ["", "api", "v1", "admin", "upgrades"]
                and parts[-1] == "unclaimed-cancellation"
            )
            or path == f"/api/v1/admin/upgrades/recovery-schema/{job}"
            or (
                len(parts) in {7, 8}
                and parts[:5] == ["", "api", "v1", "admin", "upgrades"]
                and parts[5] == "recoveries"
                and (
                    len(parts) == 7
                    or parts[-1]
                    in {
                        "cancel",
                        "close-unstarted",
                        "continuation-schema",
                        "continuation-schema-observation",
                        "host-hold",
                        "native-terminal",
                    }
                )
            )
            or (
                len(parts) == 7
                and parts[:5] == ["", "api", "v1", "internal", "upgrades"]
                and parts[-1] in NODE_SUFFIXES
            )
        )
        if not allowed:
            return JSONResponse(
                status_code=404,
                content={"detail": "Compatibility recovery route unavailable"},
                headers={"Cache-Control": "no-store"},
            )
        token = scope.set((job, generation, source))
        try:
            return await call_next(request)
        finally:
            scope.reset(token)

    return app


def main() -> None:
    uvicorn.run(
        create_recovery_support(Settings()),
        host="0.0.0.0",
        port=8000,
        proxy_headers=False,
        access_log=False,
        loop="uvloop",
    )


if __name__ == "__main__":
    main()
