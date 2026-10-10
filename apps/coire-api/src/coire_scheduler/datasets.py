"""Durable database-backed Studio CPU analysis dispatch and fenced result ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import anyio
from opentelemetry import metrics, trace
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import (
    MemoryReservationRow,
    NodeMemoryLedgerRow,
    NodeRow,
    TrainingCommandRow,
    TrainingDatasetAnalysisRow,
    TrainingDatasetGrantRow,
    TrainingDatasetRevisionRow,
    session_scope,
)
from coire_api.nodes_client import NodeClient, NodeError
from coire_api.placement.service import lock_nodes_for_admission
from coire_api.training.datasets import purge_retired_dataset
from coire_api.training.input_grants import live_analysis_source, mint_analysis_grant
from coire_api.training.quota import reconcile_upload_holds
from coire_api.training.storage import DatasetStore
from coire_core.errors import TrainingForbidden, TrainingNotFound
from coire_core.models.datasets import (
    DatasetAnalysis,
    DatasetAnalysisBinding,
    DatasetAnalysisDispatch,
    DatasetRegistrationCommand,
)
from coire_core.models.node import Reachability
from coire_core.models.placement import MemoryReservationState, ReservationHolder
from coire_core.models.training_node import (
    DatasetAnalysisWorkerInput,
    NodeAnalysisCancelRequest,
    NodeDatasetAnalysisRequest,
)
from coire_core.settings import Settings

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.scheduler.training.datasets")
transitions = metrics.get_meter("coire.scheduler.training").create_counter(
    "coire_dataset_analysis_transitions_total"
)


class DatasetAnalysisExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.task: asyncio.Task[None] | None = None
        self.stop_event = asyncio.Event()

    async def start(self) -> None:
        # Disabling new work must still cancel/reconcile existing analyses and
        # finish private cleanup. _advance gates every new dispatch separately.
        if self.task is None:
            self.stop_event.clear()
            self.task = asyncio.create_task(self.run(), name="dataset-analysis-dispatch")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.pass_once()
            except Exception as error:
                logger.error(
                    "dataset analysis scan failed", extra={"error_type": type(error).__name__}
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=2)

    async def pass_once(self) -> None:
        async with session_scope() as session:
            identities = list(
                await session.scalars(
                    select(TrainingDatasetAnalysisRow.id)
                    .where(
                        TrainingDatasetAnalysisRow.state.in_(("queued", "running")),
                    )
                    .order_by(TrainingDatasetAnalysisRow.created_at)
                    .limit(8)
                )
            )
        for identity in identities:
            try:
                await self.advance(identity)
            except Exception as error:
                logger.error(
                    "dataset analysis transition failed",
                    extra={"analysis_id": str(identity), "error_type": type(error).__name__},
                )
        async with session_scope() as session:
            await reconcile_upload_holds(session, self.settings)
            retired = list(
                await session.scalars(
                    select(TrainingDatasetRevisionRow.id)
                    .where(TrainingDatasetRevisionRow.state == "retired")
                    .limit(8)
                )
            )
        if retired:
            store = await anyio.to_thread.run_sync(DatasetStore, self.settings)
            for identity in retired:
                async with session_scope() as session:
                    await purge_retired_dataset(session, identity, store)

    async def advance(self, identity: uuid.UUID) -> None:
        # A dedicated transaction owns only this advisory lock across node I/O.
        # Domain transactions stay short so the node can fetch its source and
        # authority revocation can commit while a start is in flight.
        async with session_scope() as ownership:
            owned = await ownership.scalar(
                select(
                    func.pg_try_advisory_xact_lock(
                        func.hashtextextended(f"coire.dataset.analysis:{identity}", 0)
                    )
                )
            )
            if not owned:
                return
            await self._advance(identity)

    async def _advance(self, identity: uuid.UUID) -> None:
        with tracer.start_as_current_span("coire.scheduler.training.dataset.advance") as span:
            span.set_attribute("coire.analysis_id", str(identity))
            async with session_scope() as session:
                row = await session.get(TrainingDatasetAnalysisRow, identity)
                if row is None or row.state not in {"queued", "running"}:
                    return
                command = await session.get(TrainingCommandRow, row.command_id)
                assert command is not None
                payload = DatasetRegistrationCommand.model_validate(command.payload)
                assert payload.analysis is not None
                dispatch = (
                    DatasetAnalysisDispatch.model_validate(row.result["dispatch"])
                    if row.result and "dispatch" in row.result
                    else None
                )
                permitted = self.settings.training_enabled
                dataset: TrainingDatasetRevisionRow | None
                try:
                    dataset = await live_analysis_source(session, identity)
                except (TrainingForbidden, TrainingNotFound):
                    permitted = False
                    dataset = await session.get(TrainingDatasetRevisionRow, row.dataset_id)
                assert dataset is not None
                expired = datetime.now(UTC) >= (
                    dispatch.deadline
                    if dispatch
                    else row.created_at
                    + timedelta(seconds=self.settings.training_analysis_timeout_s)
                )
                if dispatch is None and (not permitted or expired):
                    await self.finish(session, row, dataset, None, None, "failed")
                    return
                if dispatch is None:
                    nodes = list(
                        await session.scalars(
                            select(NodeRow)
                            .where(NodeRow.name.in_(("coire-edge-a", "coire-edge-b")))
                            .order_by(NodeRow.name)
                        )
                    )
                    await lock_nodes_for_admission(session, [node.id for node in nodes])
                    for node in nodes:
                        ledger = await session.get(
                            NodeMemoryLedgerRow, node.id, populate_existing=True
                        )
                        if (
                            ledger is None
                            or ledger.health is not Reachability.HEALTHY
                            or ledger.health_sampled_at is None
                            or ledger.measured_resident_bytes is None
                            or ledger.health_sampled_at > datetime.now(UTC)
                            or (datetime.now(UTC) - ledger.health_sampled_at).total_seconds()
                            > self.settings.training_telemetry_freshness_s
                        ):
                            continue
                        holds = list(
                            await session.scalars(
                                select(MemoryReservationRow).where(
                                    MemoryReservationRow.node_id == node.id,
                                    MemoryReservationRow.state.in_(
                                        (
                                            MemoryReservationState.PENDING,
                                            MemoryReservationState.HELD,
                                            MemoryReservationState.RELEASING,
                                        )
                                    ),
                                )
                            )
                        )
                        if any(hold.holder_id.startswith("dataset-analysis:") for hold in holds):
                            continue
                        if (
                            max(sum(hold.bytes for hold in holds), ledger.measured_resident_bytes)
                            + self.settings.training_analysis_memory_bytes
                            > ledger.budget_bytes
                        ):
                            continue
                        reservation_id = uuid.uuid4()
                        session.add(
                            MemoryReservationRow(
                                id=reservation_id,
                                node_id=node.id,
                                holder_type=ReservationHolder.TRAINING,
                                holder_id=f"dataset-analysis:{identity}",
                                bytes=self.settings.training_analysis_memory_bytes,
                                pinned=True,
                                state=MemoryReservationState.HELD,
                            )
                        )
                        dispatch = DatasetAnalysisDispatch(
                            analysis_id=identity,
                            node_id=node.id,
                            node_name=cast(Literal["coire-edge-a", "coire-edge-b"], node.name),
                            reservation_id=reservation_id,
                            command_id=uuid.uuid4(),
                            deadline=row.created_at
                            + timedelta(seconds=self.settings.training_analysis_timeout_s),
                        )
                        row.result = {"dispatch": dispatch.model_dump(mode="json")}
                        row.state = "running"
                        break
                    if dispatch is None:
                        return
                envelope = (
                    DatasetAnalysisWorkerInput.model_validate(row.result["worker"])
                    if row.result and "worker" in row.result
                    else DatasetAnalysisWorkerInput(
                        command_id=dispatch.command_id,
                        analysis_id=identity,
                        binding=payload.analysis,
                        source_bytes=dataset.source_bytes,
                        memory_bytes=self.settings.training_analysis_memory_bytes,
                        max_sequence_length=self.settings.training_max_sequence_length,
                        deadline=dispatch.deadline,
                    )
                )
                if (
                    envelope.binding != payload.analysis
                    or envelope.analysis_id != identity
                    or envelope.command_id != dispatch.command_id
                    or envelope.deadline != dispatch.deadline
                    or envelope.source_bytes != dataset.source_bytes
                    or envelope.binding.model_id != row.model_id
                    or envelope.binding.variant_id != row.variant_id
                ):
                    raise ValueError("dataset analysis immutable worker binding differs")
                reservation = await session.get(MemoryReservationRow, dispatch.reservation_id)
                if (
                    reservation is None
                    or reservation.node_id != dispatch.node_id
                    or reservation.holder_id != f"dataset-analysis:{identity}"
                    or reservation.holder_type is not ReservationHolder.TRAINING
                    or reservation.bytes != envelope.memory_bytes
                    or reservation.state is not MemoryReservationState.HELD
                ):
                    raise ValueError("dataset analysis reservation ownership differs")
                row.result = {
                    "dispatch": dispatch.model_dump(mode="json"),
                    "worker": envelope.model_dump(mode="json"),
                }
                grant = (
                    await mint_analysis_grant(session, identity, dispatch.node_id, self.settings)
                    if permitted and not expired
                    else None
                )
                request = None
                if grant is not None:
                    request = NodeDatasetAnalysisRequest(
                        command_id=dispatch.command_id,
                        request_sha256=hashlib.sha256(
                            envelope.model_dump_json().encode()
                        ).hexdigest(),
                        analysis_id=identity,
                        model_id=row.model_id,
                        variant_id=row.variant_id,
                        base_manifest_sha256=payload.analysis.base_manifest_sha256,
                        template_sha256=hashlib.sha256(
                            payload.analysis.template_override.encode()
                        ).hexdigest()
                        if payload.analysis.template_override is not None
                        else None,
                        input_grant=grant,
                        reservation_id=dispatch.reservation_id,
                        memory_bytes=envelope.memory_bytes,
                        max_sequence_length=envelope.max_sequence_length,
                        deadline=dispatch.deadline,
                        binding=payload.analysis,
                    )
            # The committed dispatch/worker is crash durable before the first node call.
            # The advisory owner excludes concurrent schedulers without locking
            # rows needed by private source delivery during node start.
            async with session_scope() as session:
                current = await session.get(TrainingDatasetAnalysisRow, identity)
                if (
                    current is None
                    or current.state != "running"
                    or not current.result
                    or current.result.get("dispatch") != dispatch.model_dump(mode="json")
                ):
                    return
                async with NodeClient(self.settings) as client:
                    try:
                        observed = await client.dataset_analysis_status(
                            dispatch.node_name, identity
                        )
                    except NodeError as error:
                        if error.status != 404:
                            return
                        if request is None:
                            final_dataset = await session.get(
                                TrainingDatasetRevisionRow, current.dataset_id, with_for_update=True
                            )
                            assert final_dataset is not None
                            await self.finish(
                                session, current, final_dataset, dispatch, None, "failed"
                            )
                            return
                        observed = await client.start_dataset_analysis(dispatch.node_name, request)
                    if observed.analysis_id != identity:
                        raise ValueError("dataset analysis observation scope differs")
                    if observed.completed_rows > dataset.row_count or (
                        observed.state == "succeeded"
                        and observed.completed_rows != dataset.row_count
                    ):
                        raise ValueError("dataset analysis progress scope or counts differ")
                    if not permitted or expired:
                        if observed.state not in {"succeeded", "failed", "cancelled"}:
                            observed = await client.cancel_dataset_analysis(
                                dispatch.node_name,
                                NodeAnalysisCancelRequest(
                                    command_id=uuid.uuid5(dispatch.command_id, "cancel"),
                                    analysis_id=identity,
                                ),
                            )
                        if observed.analysis_id != identity:
                            raise ValueError("dataset analysis cancellation scope differs")
                    if observed.state not in {"succeeded", "failed", "cancelled"}:
                        return
                authority_valid = (
                    permitted and not expired and datetime.now(UTC) < dispatch.deadline
                )
                try:
                    await live_analysis_source(session, identity)
                except (TrainingForbidden, TrainingNotFound):
                    authority_valid = False
                current = await session.get(
                    TrainingDatasetAnalysisRow,
                    identity,
                    with_for_update=True,
                    populate_existing=True,
                )
                if (
                    current is None
                    or current.state != "running"
                    or not current.result
                    or current.result.get("dispatch") != dispatch.model_dump(mode="json")
                ):
                    return
                final_dataset = await session.get(
                    TrainingDatasetRevisionRow, current.dataset_id, with_for_update=True
                )
                assert final_dataset is not None
                if (
                    observed.state == "succeeded"
                    and authority_valid
                    and (observed.result is None or observed.result.state != "succeeded")
                ):
                    raise ValueError("successful dataset analysis lacks complete result")
                if (
                    observed.result is not None
                    and authority_valid
                    and payload.analysis.template_override is not None
                    and observed.result.template_sha256
                    != hashlib.sha256(payload.analysis.template_override.encode()).hexdigest()
                ):
                    raise ValueError("dataset analysis effective template differs")
                await self.finish(
                    session,
                    current,
                    final_dataset,
                    dispatch,
                    observed.result if authority_valid else None,
                    observed.state if authority_valid else "failed",
                )

    async def finish(
        self,
        session: AsyncSession,
        row: TrainingDatasetAnalysisRow,
        dataset: TrainingDatasetRevisionRow,
        dispatch: DatasetAnalysisDispatch | None,
        result: DatasetAnalysis | None,
        state: str,
    ) -> None:
        expected_samples = dataset.row_count * (2 if dataset.format == "preference" else 1)
        if result is not None:
            command = await session.get(TrainingCommandRow, row.command_id)
            if command is None:
                raise ValueError("analysis input binding is unavailable")
            result.validate_binding(
                DatasetAnalysisBinding.model_validate(command.payload.get("analysis"))
            )
        if result is not None and (
            result.id != row.id
            or result.dataset_id != dataset.id
            or result.model_id != row.model_id
            or result.variant_id != row.variant_id
            or result.state != state
            or result.row_count != dataset.row_count
            or result.invalid_count > dataset.row_count
            or result.duplicate_rows > dataset.row_count
            or any(item.row > dataset.row_count for item in result.diagnostics)
            or any(count < 0 for count in result.role_counts.values())
            or (
                result.tokens is not None
                and (
                    sum(result.tokens.histogram) > expected_samples
                    or (state == "succeeded" and sum(result.tokens.histogram) != expected_samples)
                )
            )
        ):
            raise ValueError("dataset analysis result scope or counts differ")
        if dispatch is not None:
            reservation = await session.get(
                MemoryReservationRow, dispatch.reservation_id, with_for_update=True
            )
            if (
                reservation is None
                or reservation.node_id != dispatch.node_id
                or reservation.holder_id != f"dataset-analysis:{row.id}"
                or reservation.holder_type is not ReservationHolder.TRAINING
            ):
                raise ValueError("dataset analysis reservation ownership differs")
            reservation.state = MemoryReservationState.RELEASED
            reservation.released_at = datetime.now(UTC)
        row.state = state
        transitions.add(1, {"state": state})
        logger.info(
            "dataset analysis terminal",
            extra={"analysis_id": str(row.id), "dataset_id": str(dataset.id), "state": state},
        )
        row.result = (
            result
            or DatasetAnalysis(
                id=row.id,
                dataset_id=dataset.id,
                model_id=row.model_id,
                variant_id=row.variant_id,
                state=cast(Literal["failed", "cancelled"], state),
                created_at=row.created_at,
            )
        ).model_dump(mode="json")
        if result is not None:
            row.tokenizer_sha256, row.template_sha256, row.runtime_sha256 = (
                result.tokenizer_sha256,
                result.template_sha256,
                result.runtime_sha256,
            )
            dataset.invalid_count = result.invalid_count
            dataset.diagnostics = [item.model_dump(mode="json") for item in result.diagnostics]
        dataset.state = "ready" if state == "succeeded" else "analysis_failed"
        dataset.version += 1
        await session.execute(
            update(TrainingDatasetGrantRow)
            .where(
                TrainingDatasetGrantRow.analysis_id == row.id,
                TrainingDatasetGrantRow.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
