"""One-process, private executor service and its credentialed local health probe."""

import asyncio
import hmac
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException

from coire_core.errors import CoireError
from coire_core.logging import StructuredJSONFormatter
from coire_core.models.health import ReadyResponse
from coire_core.models.upgrades import ExecutorLiveness, ExecutorPassiveReadiness
from coire_core.settings import Settings
from coire_upgrade_executor import __version__
from coire_upgrade_executor.telemetry import bootstrap_is_fresh, configure


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        from coire_upgrade_executor.supervisor import run_bootstrap_supervisor

        stop = asyncio.Event()
        supervisor = asyncio.create_task(run_bootstrap_supervisor(application, settings, stop))
        application.state.bootstrap_supervisor = supervisor
        try:
            yield
        finally:
            stop.set()
            await supervisor
            application.state.bootstrap_supervisor = None

    app = FastAPI(
        title="Coire upgrade executor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # Bootstrap must provide a verified independent recovery owner before this
    # can become true; enabling the feature is insufficient evidence.
    app.state.bootstrap_verified = False
    app.state.bootstrap_verified_at = None
    app.state.bootstrap_authority = None
    app.state.bootstrap_supervisor = None
    app.state.passive_observer = None

    def require_credential(authorization: str | None) -> None:
        token = settings.upgrade_executor_token.get_secret_value()
        supplied = (
            authorization.removeprefix("Bearer ")
            if authorization and authorization.startswith("Bearer ")
            else ""
        )
        if not token or not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(
                401, "executor credential required", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.get("/live", response_model=ExecutorLiveness)
    async def live(
        authorization: Annotated[str | None, Header(max_length=4096)] = None,
    ) -> ExecutorLiveness:
        require_credential(authorization)
        supervisor = app.state.bootstrap_supervisor
        if supervisor is None or supervisor.done():
            raise HTTPException(503, "independent recovery supervisor is not running")
        return ExecutorLiveness(version=__version__)

    @app.get("/passive", response_model=ExecutorPassiveReadiness)
    async def passive(
        authorization: Annotated[str | None, Header(max_length=4096)] = None,
    ) -> ExecutorPassiveReadiness:
        require_credential(authorization)
        observer = app.state.passive_observer
        if not settings.upgrades_enabled or observer is None:
            raise HTTPException(503, "passive candidate protocol is unavailable")
        try:
            return ExecutorPassiveReadiness.model_validate(await observer.observe())
        except (CoireError, OSError, ValueError):
            raise HTTPException(503, "passive candidate protocol is unverified") from None

    @app.get("/ready", response_model=ReadyResponse)
    async def ready(
        authorization: Annotated[str | None, Header(max_length=4096)] = None,
    ) -> ReadyResponse:
        require_credential(authorization)
        if not settings.upgrades_enabled or not bootstrap_is_fresh(
            app.state.bootstrap_verified, app.state.bootstrap_verified_at
        ):
            raise HTTPException(503, "independent recovery bootstrap is not ready")
        if app.state.bootstrap_authority is None:
            raise HTTPException(503, "independent recovery writer is not held")
        try:
            app.state.bootstrap_authority.verify()
        except (CoireError, OSError, ValueError):
            raise HTTPException(503, "independent recovery writer is unavailable") from None
        return ReadyResponse(service="coire-upgrade-executor", version=__version__)

    return app


def main() -> int:
    settings = Settings()
    if sys.argv[1:] == ["--healthcheck"]:
        from coire_upgrade_executor.healthcheck import check

        return check(settings)
    if len(sys.argv) != 1:
        return 2
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredJSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    configure(settings)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8011, workers=1, access_log=False)
    return 0
