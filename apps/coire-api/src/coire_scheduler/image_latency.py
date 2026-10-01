"""Withdraw measured image/chat coexistence when live same-node latency regresses."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from datetime import UTC, datetime

import httpx
from opentelemetry import metrics, trace
from pydantic import ValidationError
from sqlalchemy import select

from coire_api.audit import write_audit
from coire_api.db import ImageCoexistenceProfileRow, ImageJobRow, NodeRow, session_scope
from coire_api.images.quota import _QUOTA_LOCK
from coire_core.models.audit import AuditOutcome
from coire_core.models.images import ImageJobState, ImageLatencyQueryResponse
from coire_scheduler.image_admission import node_thermal_alarm

PROMETHEUS_URL = "http://prometheus:9090"
_NODE = re.compile(r"coire-[a-z0-9-]{1,50}\Z")
_P95_LIMIT_MS = 1500.0
_QUERY_SECONDS = 1.5
_POLL_SECONDS = 15.0
_tracer = trace.get_tracer("coire.scheduler.image_latency")
_meter = metrics.get_meter("coire.scheduler.image_latency")
latency_monitor_total = _meter.create_counter(
    "coire_image_latency_monitor_total",
    unit="1",
    description="Live same-node chat latency observations and coexistence revocations",
)
logger = logging.getLogger(__name__)


class ImageLatencyUnavailable(RuntimeError):
    """The live metrics response cannot be trusted for admission."""


async def query_node_first_token_p95_ms(
    node: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> float | None:
    """Return a current five-minute p95, or None when no chat samples exist."""
    if _NODE.fullmatch(node) is None:
        raise ImageLatencyUnavailable("invalid Studio label")
    query = (
        "histogram_quantile(0.95, sum by (le) "
        f'(rate(coire_gateway_first_token_duration_ms_bucket{{node="{node}"}}[5m])))'
    )
    try:
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=_QUERY_SECONDS
        ) as client:
            response = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
            response.raise_for_status()
            if len(response.content) > 16 * 1024:
                raise ImageLatencyUnavailable("metrics response too large")
            parsed = ImageLatencyQueryResponse.model_validate(response.json())
        if not parsed.data.result:
            return None
        value = float(parsed.data.result[0].value[1])
        if not math.isfinite(value) or value < 0:
            raise ImageLatencyUnavailable("invalid latency sample")
        return value
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        raise ImageLatencyUnavailable("live latency metrics unavailable") from exc


async def invalidate_regressed_node(
    node_id: uuid.UUID,
    *,
    p95_ms: float | None,
    thermal_alarm: bool = False,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Withdraw approval on regression or an unobservable approved mix."""
    if (
        not thermal_alarm
        and p95_ms is not None
        and (not math.isfinite(p95_ms) or p95_ms <= _P95_LIMIT_MS)
    ):
        return 0, 0
    reason = (
        "thermal_alarm"
        if thermal_alarm
        else "chat_latency_regression"
        if p95_ms is not None
        else "latency_metrics_unavailable"
    )
    current = now or datetime.now(UTC)
    async with session_scope() as session:
        await session.execute(_QUOTA_LOCK)
        profiles = (
            await session.scalars(
                select(ImageCoexistenceProfileRow)
                .where(
                    ImageCoexistenceProfileRow.node_id == node_id,
                    ImageCoexistenceProfileRow.status == "approved",
                    ImageCoexistenceProfileRow.invalidated_at.is_(None),
                    ImageCoexistenceProfileRow.valid_until > current,
                )
                .with_for_update()
            )
        ).all()
        for profile in profiles:
            profile.status = "invalidated"
            profile.invalidated_at = current
            await write_audit(
                session,
                actor="coire-scheduler",
                action=(
                    "image.coexistence.thermal_invalidated"
                    if thermal_alarm
                    else "image.coexistence.latency_invalidated"
                    if p95_ms is not None
                    else "image.coexistence.monitor_invalidated"
                ),
                target_type="image_coexistence_profile",
                target_id=str(profile.id),
                outcome=AuditOutcome.OK,
                context={
                    "node_id": str(node_id),
                    "reason": reason,
                    **({"first_token_p95_ms": round(p95_ms, 3)} if p95_ms is not None else {}),
                },
            )
        jobs = (
            await session.scalars(
                select(ImageJobRow)
                .where(
                    ImageJobRow.selected_node_id == node_id,
                    ImageJobRow.state.in_((ImageJobState.RESERVING, ImageJobState.RUNNING)),
                    ImageJobRow.cancel_requested_at.is_(None),
                )
                .with_for_update()
            )
        ).all()
        for job in jobs:
            job.state = ImageJobState.CANCELLING
            job.cancel_requested_at = current
            job.updated_at = current
            job.version += 1
            await write_audit(
                session,
                actor="coire-scheduler",
                action="image.latency_cancel",
                target_type="image_job",
                target_id=job.id,
                outcome=AuditOutcome.OK,
                context={"node_id": str(node_id), "reason": reason},
            )
        return len(profiles), len(jobs)


async def monitor_image_latency_once(*, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Observe only nodes with current approved profiles, outside a DB transaction."""
    now = datetime.now(UTC)
    async with session_scope() as session:
        nodes = (
            await session.execute(
                select(NodeRow.id, NodeRow.name)
                .join(ImageCoexistenceProfileRow, ImageCoexistenceProfileRow.node_id == NodeRow.id)
                .where(
                    ImageCoexistenceProfileRow.status == "approved",
                    ImageCoexistenceProfileRow.invalidated_at.is_(None),
                    ImageCoexistenceProfileRow.valid_until > now,
                )
                .distinct()
            )
        ).all()
    for node_id, name in nodes:
        with _tracer.start_as_current_span("coire.scheduler.image.latency_monitor") as span:
            span.set_attribute("coire.node_id", str(node_id))
            async with session_scope() as session:
                thermal = await node_thermal_alarm(session, node_id, now)
            if thermal:
                profiles, jobs = await invalidate_regressed_node(
                    node_id, p95_ms=None, thermal_alarm=True
                )
                latency_monitor_total.add(1, {"outcome": "thermal_alarm"})
                logger.warning(
                    "image coexistence thermal alarm; approvals withdrawn and stops requested",
                    extra={"node_id": str(node_id), "profile_count": profiles, "job_count": jobs},
                )
                continue
            try:
                p95_ms = await query_node_first_token_p95_ms(name, transport=transport)
            except ImageLatencyUnavailable:
                latency_monitor_total.add(1, {"outcome": "unavailable"})
                profiles, jobs = await invalidate_regressed_node(node_id, p95_ms=None)
                logger.warning(
                    "image coexistence latency unavailable; approvals withdrawn",
                    extra={"node_id": str(node_id), "profile_count": profiles, "job_count": jobs},
                )
                continue
            if p95_ms is None:
                latency_monitor_total.add(1, {"outcome": "no_sample"})
                continue
            if p95_ms <= _P95_LIMIT_MS:
                latency_monitor_total.add(1, {"outcome": "healthy"})
                continue
            profiles, jobs = await invalidate_regressed_node(node_id, p95_ms=p95_ms)
            latency_monitor_total.add(1, {"outcome": "regressed"})
            logger.warning(
                "image coexistence latency regressed; approvals withdrawn and stops requested",
                extra={"node_id": str(node_id), "profile_count": profiles, "job_count": jobs},
            )


async def monitor_image_latency(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await monitor_image_latency_once()
        except Exception as exc:
            latency_monitor_total.add(1, {"outcome": "failed"})
            logger.error("image latency monitor failed error_type=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
        except TimeoutError:
            continue
