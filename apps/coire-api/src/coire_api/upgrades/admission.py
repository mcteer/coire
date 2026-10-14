"""Track admitted Core work through streaming while durable scope fences exclude new work."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from coire_api.db import UpgradeMaintenanceRow, session_scope
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import UpgradeInventoryEntry, UpgradeOwnerInventory

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_IMAGE_JOB = r"[0-9A-HJKMNP-TV-Z]{26}"


def affected_services(method: str, path: str) -> tuple[str, ...]:
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return ()
    if method == "DELETE" and (
        re.fullmatch(r"/api/v1/admin/(?:keys|runs)/" + _UUID, path)
        or re.fullmatch(r"/api/v1/images/" + _IMAGE_JOB, path)
    ):
        # Revocation, the admin kill switch and owner image cancellation still
        # execute their ordinary route authentication, scope and audit checks.
        return ()
    # Completion/cleanup/control commands remain callable during drain.
    if path.endswith(("/cancel", "/stop", "/cleanup", "/lease", "/renew", "/release")):
        return ()
    if path.startswith(("/api/v1/internal/", "/api/v1/admin/upgrades", "/api/v1/nodes/")):
        return ()
    if path.startswith(("/mcp", "/api/v1/mcp")):
        return ("coire-api", "coire-scheduler", "coire-mcp")
    if method == "GET":
        # Track the full ASGI response, including event and artifact streams.
        # Health and upgrade progress remain reachable during maintenance.
        return ("coire-api",) if path.startswith(("/api/v1/", "/v1/")) else ()
    if path.startswith("/api/v1/images/inputs"):
        return ("coire-api", "coire-scheduler", "coire-file-worker")
    if path.startswith("/api/v1/admin/ops"):
        return ("coire-api", "coire-ops")
    if path.startswith(("/v1/", "/api/v1/chat/", "/api/v1/feedback/", "/api/v1/comparisons")):
        return ("coire-api", "coire-scheduler") + (
            ("coire-file-worker",)
            if "/files" in path or "/attachments" in path or method == "DELETE"
            else ()
        )
    if path.startswith(
        (
            "/api/v1/runs",
            "/api/v1/admin/runs",
            "/api/v1/admin/training",
            "/api/v1/admin/evaluations",
            "/api/v1/admin/images",
            "/api/v1/admin/feedback",
            "/api/v1/admin/models",
        )
    ):
        return ("coire-api", "coire-scheduler")
    return ("coire-api",)


class CoreAdmissions:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self._lock = asyncio.Lock()
        self._active: dict[tuple[str, ...], int] = {}

    @asynccontextmanager
    async def admit(self, services: tuple[str, ...]) -> AsyncIterator[None]:
        if not self.enabled or not services:
            yield
            return
        async with self._lock:
            self._active[services] = self._active.get(services, 0) + 1
        try:
            async with session_scope() as session:
                fenced = await session.scalar(
                    select(UpgradeMaintenanceRow.target)
                    .where(
                        UpgradeMaintenanceRow.target.in_(("cluster", "core", *services)),
                        UpgradeMaintenanceRow.released.is_(False),
                    )
                    .limit(1)
                )
            if fenced is not None:
                raise UpgradeUnavailable(
                    "Control plane is draining for an upgrade; retry after maintenance"
                )
            yield
        finally:
            async with self._lock:
                self._active[services] -= 1
                if not self._active[services]:
                    del self._active[services]

    async def inventory(
        self, services: tuple[str, ...], *, owner: str = "requests"
    ) -> UpgradeOwnerInventory:
        async with self._lock:
            active = sum(
                count for scope, count in self._active.items() if set(scope) & set(services)
            )
        return UpgradeOwnerInventory(
            owner=owner,
            status="complete",
            entries=()
            if not active
            else (
                UpgradeInventoryEntry(
                    owner=owner, kind="core-requests", work_id="inflight", state="active"
                ),
            ),
        )


class CoreAdmissionMiddleware:
    def __init__(self, app: ASGIApp, admissions: CoreAdmissions) -> None:
        self.app = app
        self.admissions = admissions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        services = affected_services(scope["method"], scope["path"])
        permit = self.admissions.admit(services)
        try:
            await permit.__aenter__()
        except UpgradeUnavailable as error:
            await JSONResponse(
                error.to_problem().model_dump(mode="json"),
                status_code=error.status,
                media_type="application/problem+json",
                headers={"Retry-After": "5"},
            )(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            await permit.__aexit__(None, None, None)
