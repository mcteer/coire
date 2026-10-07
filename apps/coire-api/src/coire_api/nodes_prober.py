"""Background node prober (T058).

Probes each registered node over the control fabric and maintains `reachability`. Feature 000 sets only
HEALTHY / UNREACHABLE / UNKNOWN; the damped `degraded` state, hysteresis and health-record
freshness windows belong to feature 009.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coire_api.db import (
    MemoryReservationRow,
    NodeMemoryLedgerRow,
    NodeRow,
    TrainingAttemptRow,
    TrainingJobRow,
    TrainingParticipantRow,
    create_engine,
)
from coire_api.placement.service import (
    drift_ratio,
    image_residency_unavailable,
    ledger_drift,
)
from coire_api.training.telemetry import observed
from coire_core.models.engine import LIVE_ENGINE_STATES, EngineState
from coire_core.models.node import NodeStatus, NodeStatusV2, Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.net import ControlClient
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


def measured_node_residency(
    status: NodeStatus | NodeStatusV2,
    *,
    image_reserved_bytes: int,
    training_reserved_bytes: int = 0,
) -> int | None:
    """Include the image child's physical footprint without inventing an unknown value."""
    if any(
        engine.resident_bytes is None
        and (engine.state in LIVE_ENGINE_STATES or engine.state is EngineState.ORPHAN)
        for engine in status.engines
    ):
        return None
    if image_reserved_bytes > 0 and status.image_worker_resident_bytes is None:
        return None
    training = getattr(status, "training", [])
    if training_reserved_bytes and not training:
        return None
    if any(
        attempt.liveness in {"running", "stopping", "unknown", "orphan"}
        and attempt.footprint_bytes is None
        for attempt in training
    ):
        return None
    return (
        sum(attempt.footprint_bytes or 0 for attempt in training if attempt.liveness != "stopped")
        + sum(engine.resident_bytes or 0 for engine in status.engines)
        + (status.image_worker_resident_bytes or 0)
    )


class NodeProber:
    """Polls `/node/health` on every registered node and records what it finds."""

    def __init__(self, settings: Settings, reconciler: object | None = None) -> None:
        self._settings = settings
        self._reconciler = reconciler
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._link_probe_coordinator: object | None = None

    def set_reconciler(self, reconciler: object) -> None:
        self._reconciler = reconciler

    def set_link_probe_coordinator(self, coordinator: object) -> None:
        self._link_probe_coordinator = coordinator

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="node-prober")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        engine = create_engine(self._settings)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            while not self._stopping.is_set():
                try:
                    await self._probe_once(maker)
                except Exception:
                    logger.exception("node probe cycle failed; will retry next interval")
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._settings.node_probe_interval_s
                    )
                except TimeoutError:
                    continue
        finally:
            await engine.dispose()

    @observed("coire.api.training.node_observation")
    async def _probe_once(self, maker: async_sessionmaker[AsyncSession]) -> None:
        tokens = self._settings.node_token_map
        async with maker() as session:
            rows = list((await session.execute(select(NodeRow))).scalars().all())
            if not rows:
                return
            async with ControlClient(timeout=5.0) as client:
                for row in rows:
                    status = await self._probe_node(client, row, tokens.get(row.name, ""))
                    row.health_observed_at = datetime.now(UTC)
                    row.gpu_percent = status.gpu_percent if status is not None else None
                    ledger = await session.get(NodeMemoryLedgerRow, row.id)
                    if ledger is not None:
                        ledger.health = row.reachability
                        ledger.health_reason = (
                            None if status is not None else "node health probe failed"
                        )
                        ledger.health_sampled_at = (
                            status.sampled_at
                            if status is not None and status.sampled_at.tzinfo is not None
                            else None
                        )
                        if status is not None and (
                            ledger.health_sampled_at is None
                            or not datetime.now(UTC) - timedelta(seconds=60)
                            <= ledger.health_sampled_at
                            <= datetime.now(UTC)
                        ):
                            ledger.health, ledger.health_reason = (
                                Reachability.UNKNOWN,
                                "stale_health",
                            )
                        if status is not None:
                            for training in getattr(status, "training", []):
                                participant = await session.scalar(
                                    select(TrainingParticipantRow)
                                    .join(
                                        TrainingAttemptRow,
                                        TrainingAttemptRow.id == TrainingParticipantRow.attempt_id,
                                    )
                                    .join(
                                        TrainingJobRow,
                                        TrainingJobRow.id == TrainingAttemptRow.job_id,
                                    )
                                    .where(
                                        TrainingParticipantRow.node_id == row.id,
                                        TrainingAttemptRow.id == training.attempt_id,
                                        TrainingAttemptRow.fence == training.fence,
                                        TrainingJobRow.fence == TrainingAttemptRow.fence,
                                        TrainingAttemptRow.state.in_(
                                            ["preparing", "running", "stopping", "unknown"]
                                        ),
                                    )
                                )
                                if participant is not None:
                                    participant.footprint_bytes = training.footprint_bytes
                            resident_reserved = await session.scalar(
                                select(
                                    func.coalesce(func.sum(MemoryReservationRow.bytes), 0)
                                ).where(
                                    MemoryReservationRow.node_id == row.id,
                                    MemoryReservationRow.holder_type.in_(
                                        (
                                            ReservationHolder.MODEL,
                                            ReservationHolder.IMAGE,
                                            ReservationHolder.TRAINING,
                                        )
                                    ),
                                    MemoryReservationRow.state.in_(
                                        [
                                            MemoryReservationState.PENDING,
                                            MemoryReservationState.HELD,
                                            MemoryReservationState.RELEASING,
                                        ]
                                    ),
                                )
                            )
                            image_reserved = await session.scalar(
                                select(
                                    func.coalesce(func.sum(MemoryReservationRow.bytes), 0)
                                ).where(
                                    MemoryReservationRow.node_id == row.id,
                                    MemoryReservationRow.holder_type == ReservationHolder.IMAGE,
                                    MemoryReservationRow.state.in_(
                                        [
                                            MemoryReservationState.PENDING,
                                            MemoryReservationState.HELD,
                                            MemoryReservationState.RELEASING,
                                        ]
                                    ),
                                )
                            )
                            ledger.measured_resident_bytes = measured_node_residency(
                                status,
                                image_reserved_bytes=int(image_reserved or 0),
                                training_reserved_bytes=int(
                                    await session.scalar(
                                        select(
                                            func.coalesce(func.sum(MemoryReservationRow.bytes), 0)
                                        ).where(
                                            MemoryReservationRow.node_id == row.id,
                                            MemoryReservationRow.holder_type
                                            == ReservationHolder.TRAINING,
                                            MemoryReservationRow.state.in_(
                                                [
                                                    MemoryReservationState.PENDING,
                                                    MemoryReservationState.HELD,
                                                    MemoryReservationState.RELEASING,
                                                ]
                                            ),
                                        )
                                    )
                                    or 0
                                ),
                            )
                            swap = getattr(status, "swap_used_bytes", None)
                            previous_swap = getattr(ledger, "swap_used_bytes", None)
                            if hasattr(ledger, "swap_used_bytes"):
                                ledger.swap_used_bytes = swap
                            if (
                                isinstance(swap, int)
                                and isinstance(previous_swap, int)
                                and swap > previous_swap
                            ):
                                ledger.health, ledger.health_reason = (
                                    Reachability.DEGRADED,
                                    "swap_growth",
                                )
                            if (
                                ledger.measured_resident_bytes is not None
                                and ledger.measured_resident_bytes > ledger.budget_bytes
                            ):
                                ledger.health, ledger.health_reason = (
                                    Reachability.DEGRADED,
                                    "memory_breach",
                                )
                            image_residency_unavailable.set(
                                int(
                                    int(image_reserved or 0) > 0
                                    and ledger.measured_resident_bytes is None
                                ),
                                {"node": row.name},
                            )
                            drift = drift_ratio(
                                reserved_bytes=int(resident_reserved or 0),
                                measured_bytes=ledger.measured_resident_bytes,
                            )
                            ledger_drift.set(
                                drift if drift is not None else 0.0, {"node": row.name}
                            )
                        ledger.cpu_percent = status.cpu_percent if status is not None else None
                        ledger.thermal_state = (
                            status.thermal_state.value if status is not None else None
                        )
                        ledger.updated_at = datetime.now(UTC)
            await session.commit()

    async def _probe_node(
        self, client: ControlClient, row: NodeRow, token: str
    ) -> NodeStatus | NodeStatusV2 | None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = await client.get(
                row.name,
                "/node/health",
                port=self._settings.node_listen_port,
                headers=headers,
            )
            ok = resp.status_code == 200
            if ok:
                body = resp.json()
                status: NodeStatus | NodeStatusV2 | None = (
                    NodeStatusV2.model_validate(body)
                    if body.get("path") == "control"
                    else NodeStatus.model_validate(body)
                )
                if status is not None and status.name != row.name:
                    logger.warning(
                        "probe of %s returned mismatched node name %s", row.name, status.name
                    )
                    ok = False
                    status = None
            else:
                status = None
            if not ok and resp.status_code != 200:
                logger.warning("probe of %s returned HTTP %d", row.name, resp.status_code)
        except Exception as exc:
            logger.warning("probe of %s failed: %s", row.name, exc)
            ok = False

        if ok:
            assert status is not None
            # Registration is one-time, but an immutable agent rollout changes
            # the runtime fingerprint used by image coexistence admission.
            row.agent_version = status.agent_version
            recovered = row.reachability in {Reachability.UNKNOWN, Reachability.UNREACHABLE}
            # A sharded rank failure is a semantic degradation, not a transport failure.
            # Successful /health probes must not erase it; a later successful group launch
            # clears it after the rank has done real work again.
            if row.reachability is not Reachability.DEGRADED:
                row.reachability = Reachability.HEALTHY
            row.probe_failures = 0
            row.last_seen_at = datetime.now(UTC)
            if recovered and self._reconciler is not None:
                # A node that has come back may be running something the registry does not
                # know about, or may have lost something it does (spec FR-015).
                logger.info("node %s recovered; requesting a reconcile", row.name)
                self._reconciler.request_reconcile(row.name)  # type: ignore[attr-defined]
            if self._link_probe_coordinator is not None:
                self._link_probe_coordinator.observe(row.name, status)  # type: ignore[attr-defined]
            return status

        row.probe_failures += 1
        if row.probe_failures >= self._settings.node_probe_failures_before_unreachable:
            if row.reachability is not Reachability.UNREACHABLE:
                logger.error(
                    "node %s unreachable after %d consecutive failures",
                    row.name,
                    row.probe_failures,
                )
            row.reachability = Reachability.UNREACHABLE
        return None
