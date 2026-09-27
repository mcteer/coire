"""Persistence and projection for append-only Studio link evidence."""

from __future__ import annotations

import logging

from opentelemetry import metrics
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import LinkObservationRow, NodeRow
from coire_core.models import LinkObservation, StudioLinkProjection
from coire_core.settings import Settings
from coire_scheduler.sharding import project_link

meter = metrics.get_meter("coire.api.sharding")
link_last_success = meter.create_gauge(
    "coire_sharding_link_last_success_timestamp_seconds", unit="s"
)
link_eligible = meter.create_gauge("coire_sharding_link_eligible", unit="1")
link_flapping = meter.create_gauge("coire_sharding_link_flapping", unit="1")
logger = logging.getLogger(__name__)


async def append_observation(
    session: AsyncSession, observation: LinkObservation
) -> LinkObservationRow:
    nodes = {
        row.name: row
        for row in (
            await session.execute(
                select(NodeRow).where(NodeRow.name.in_([observation.node_a, observation.node_b]))
            )
        )
        .scalars()
        .all()
    }
    if set(nodes) != {observation.node_a, observation.node_b}:
        raise LookupError("both declared Studios are required")
    row = LinkObservationRow(
        id=observation.id,
        node_a_id=nodes[observation.node_a].id,
        node_b_id=nodes[observation.node_b].id,
        transport=observation.transport,
        outcome=observation.outcome,
        bandwidth_bytes_per_second=observation.bandwidth_bytes_per_second,
        latency_ms=observation.latency_ms,
        os_version_a=observation.os_version_a,
        os_version_b=observation.os_version_b,
        engine_version=observation.engine_version,
        reason=observation.reason,
        observed_at=observation.observed_at,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "Studio link observation recorded observation_id=%s transport=%s outcome=%s "
        "node_a=%s node_b=%s latency_ms=%s bandwidth_bytes_per_second=%s",
        row.id,
        row.transport.value,
        row.outcome.value,
        observation.node_a,
        observation.node_b,
        row.latency_ms,
        row.bandwidth_bytes_per_second,
    )
    return row


async def link_projection(session: AsyncSession, settings: Settings) -> StudioLinkProjection:
    nodes = {
        row.id: row.name
        for row in (
            await session.execute(
                select(NodeRow).where(NodeRow.name.in_(["coire-edge-a", "coire-edge-b"]))
            )
        ).scalars()
    }
    rows = (
        (
            await session.execute(
                select(LinkObservationRow)
                .order_by(LinkObservationRow.observed_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    observations = [
        LinkObservation(
            id=row.id,
            node_a=nodes.get(row.node_a_id, "unknown-a"),
            node_b=nodes.get(row.node_b_id, "unknown-b"),
            transport=row.transport,
            outcome=row.outcome,
            bandwidth_bytes_per_second=row.bandwidth_bytes_per_second,
            latency_ms=row.latency_ms,
            os_version_a=row.os_version_a,
            os_version_b=row.os_version_b,
            engine_version=row.engine_version,
            reason=row.reason,
            observed_at=row.observed_at,
        )
        for row in rows
        if row.node_a_id in nodes and row.node_b_id in nodes
    ]
    projection = project_link(
        observations,
        freshness_s=settings.link_probe_freshness_s,
        failures_before_down=settings.link_failures_before_down,
        successes_before_up=settings.link_successes_before_up,
    )
    link_eligible.set(1 if projection.tp_eligible else 0, {"mode": "tp"})
    link_eligible.set(1 if projection.fallback_state.value == "up" else 0, {"mode": "pp"})
    link_flapping.set(1 if projection.flapping else 0)
    for transport in {item.transport for item in observations}:
        latest_success = next(
            (
                item
                for item in observations
                if item.transport is transport and item.outcome.value == "succeeded"
            ),
            None,
        )
        if latest_success is not None:
            link_last_success.set(
                latest_success.observed_at.timestamp(), {"transport": transport.value}
            )
    return projection
