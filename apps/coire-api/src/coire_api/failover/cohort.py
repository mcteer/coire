"""Read authoritative recovery readiness without changing health or 019 ownership."""

import asyncio

import httpx
from sqlalchemy import select, text

from coire_api.db import UpgradeMaintenanceRow, session_scope
from coire_api.upgrades.core_targets import core_target_status
from coire_core.failover_telemetry import tracer
from coire_core.settings import Settings


async def authoritative_cohort_ready(settings: Settings, client: httpx.AsyncClient) -> bool:
    with tracer.start_as_current_span("coire.api.failover_cohort_ready"):
        try:
            async with asyncio.timeout(1):
                async with session_scope() as session:
                    await session.execute(text("SELECT 1"))
                    fenced = await session.scalar(
                        select(UpgradeMaintenanceRow.target)
                        .where(
                            UpgradeMaintenanceRow.released.is_(False),
                            UpgradeMaintenanceRow.target.in_(
                                (
                                    "cluster",
                                    "core",
                                    "coire-api",
                                    "coire-scheduler",
                                    "coire-mcp",
                                    "coire-ops",
                                    "coire-web",
                                    "coire-file-worker",
                                )
                            ),
                        )
                        .limit(1)
                    )
                    if fenced is not None:
                        return False
                    if settings.upgrades_enabled:
                        target = await core_target_status(session, settings, "ready")
                        if target.readiness != "ready":
                            return False
                endpoints = ["http://coire-scheduler:8002/ready"]
                if settings.mcp_enabled:
                    endpoints.append("http://coire-mcp:8001/ready")
                if settings.ops_enabled:
                    endpoints.append("http://coire-ops:8003/ready")
                replies = await asyncio.gather(*(client.get(url, timeout=0.5) for url in endpoints))
                return all(reply.status_code == 200 for reply in replies)
        except Exception:
            # No synthesized heartbeat failures and no ownership transfer. Admission and
            # hand-back remain closed until a later bounded observation is authoritative.
            return False
