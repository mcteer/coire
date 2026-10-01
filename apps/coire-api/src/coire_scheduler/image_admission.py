"""Measured chat/image coexistence admission.

Placement stays in ``choose_image_node`` (Studio B is the preferred image node).
This module only decides whether resident chat variants may share that node
with an image model. An empty resident set is allowed. Any other mix is
allowed only when an approved, unexpired, non-invalidated profile for the
node and image model covers every resident variant.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageCoexistenceProfileRow, NodeMemoryLedgerRow, NodeRow
from coire_core.models.images import ImageCoexistenceReportRequest
from coire_core.models.node import NodeRole, Reachability

PINNED_RUNTIME_VERSION = "mflux-0.20.0"
FIRST_TOKEN_P95_LIMIT_S = 1.5
MIN_MEASURED_SECONDS = 15 * 60
THERMAL_SAMPLE_MAX_AGE_S = 30


def node_hardware_fingerprint(node: NodeRow) -> str:
    """Bind approval to the node's declared physical identity and capacity."""
    values = [node.name, node.memory_total_bytes, node.gpu_cores]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def node_runtime_fingerprint(node: NodeRow) -> str:
    """An agent or pinned image runtime version change invalidates measured approval."""
    values = [node.agent_version, PINNED_RUNTIME_VERSION]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def image_environment_fingerprint(node: NodeRow, manifest_sha256: str) -> str:
    """Bind a recipe to declared Studio hardware, pinned runtime and base bytes."""
    values = [
        node_hardware_fingerprint(node),
        node_runtime_fingerprint(node),
        manifest_sha256,
    ]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


async def node_thermal_alarm(session: AsyncSession, node_id: uuid.UUID, now: datetime) -> bool:
    """Only a recent serious/critical Studio sample trips image admission."""
    ledger = await session.get(NodeMemoryLedgerRow, node_id)
    return bool(
        ledger is not None
        and ledger.thermal_state in {"serious", "critical"}
        and ledger.health_sampled_at is not None
        and 0 <= (now - ledger.health_sampled_at).total_seconds() <= THERMAL_SAMPLE_MAX_AGE_S
    )


def coexistence_report_hash(report: ImageCoexistenceReportRequest) -> str:
    canonical = report.model_copy(
        update={"chat_variant_ids": tuple(sorted(report.chat_variant_ids))}
    )
    return hashlib.sha256(
        json.dumps(
            canonical.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CoexistenceReport:
    """Benchmark evidence required before a coexistence mix may be approved."""

    chat_variant_ids: tuple[str, ...]
    first_token_p95_s: float | None
    image_progress_observed: bool
    measured_at: datetime
    runtime_version: str


def profile_covers(resident: set[str], allowed: set[str]) -> bool:
    """True when every resident chat variant was part of the measured mix."""
    return resident <= allowed


def validate_coexistence_report(report: CoexistenceReport) -> bool:
    """True only for a passing, current-runtime report.

    Missing latency, a first-token p95 above 1.5 seconds, missing image
    progress, or any runtime other than ``mflux-0.20.0`` must not be approved.
    """
    latency = report.first_token_p95_s
    if (
        latency is None
        or isinstance(latency, bool)
        or not isinstance(latency, int | float)
        or not math.isfinite(latency)
        or latency < 0
        or latency > FIRST_TOKEN_P95_LIMIT_S
    ):
        return False
    if report.image_progress_observed is not True:
        return False
    return report.runtime_version == PINNED_RUNTIME_VERSION


def _profile_authorizes(
    profile: ImageCoexistenceProfileRow,
    resident_variant_ids: set[str],
    now: datetime,
) -> bool:
    if profile.status != "approved" or profile.invalidated_at is not None:
        return False
    if profile.valid_until.tzinfo is None or now.tzinfo is None or profile.valid_until <= now:
        return False
    try:
        report = ImageCoexistenceReportRequest.model_validate(profile.benchmark_result)
    except ValueError:
        return False
    if (
        report.duration_seconds < MIN_MEASURED_SECONDS
        or report.runtime_version != PINNED_RUNTIME_VERSION
        or report.node_id != profile.node_id
        or report.image_model_id != profile.image_model_id
        or [str(item) for item in report.chat_variant_ids] != profile.chat_variant_ids
        or report.hardware_fingerprint != profile.hardware_fingerprint
        or report.runtime_fingerprint != profile.runtime_fingerprint
        or report.image_mode != profile.image_mode
        or report.measured_bounds.model_dump(mode="json") != profile.measured_bounds
        or report.first_token_p95_ms != profile.first_token_p95_ms
        or report.valid_until != profile.valid_until
        or report.measured_at > now
        or coexistence_report_hash(report) != profile.profile_hash
    ):
        return False
    latency_ms = profile.first_token_p95_ms
    if (
        isinstance(latency_ms, bool)
        or not isinstance(latency_ms, int | float)
        or not math.isfinite(latency_ms)
        or not 0 <= latency_ms <= FIRST_TOKEN_P95_LIMIT_S * 1000
    ):
        return False
    allowed: object = profile.chat_variant_ids
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        return False
    return profile_covers(resident_variant_ids, set(allowed))


async def chat_mix_allowed(
    session: AsyncSession,
    node_id: uuid.UUID,
    image_model_id: uuid.UUID,
    resident_variant_ids: set[str],
    now: datetime,
) -> bool:
    """True when an image job may share ``node_id`` with the resident chat variants.

    Inverse of an unmeasured mix. A node with no resident language instances
    is allowed. A resident set is allowed only when an approved profile for
    this node and image model still covers it.
    """
    if not resident_variant_ids:
        return True
    node = await session.get(NodeRow, node_id, populate_existing=True)
    if (
        node is None
        or node.role is not NodeRole.STUDIO
        or node.reachability is not Reachability.HEALTHY
    ):
        return False
    profiles = (
        await session.scalars(
            select(ImageCoexistenceProfileRow).where(
                ImageCoexistenceProfileRow.node_id == node_id,
                ImageCoexistenceProfileRow.image_model_id == image_model_id,
                ImageCoexistenceProfileRow.status == "approved",
                ImageCoexistenceProfileRow.invalidated_at.is_(None),
                ImageCoexistenceProfileRow.valid_until > now,
            )
        )
    ).all()
    hardware = node_hardware_fingerprint(node)
    runtime = node_runtime_fingerprint(node)
    return any(
        profile.hardware_fingerprint == hardware
        and profile.runtime_fingerprint == runtime
        and _profile_authorizes(profile, resident_variant_ids, now)
        for profile in profiles
    )
