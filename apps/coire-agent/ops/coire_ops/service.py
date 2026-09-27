"""Session lifecycle and model-backed turn orchestration for coire-ops."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress

import httpx
from opentelemetry import metrics, trace

from coire_core.models.ops import (
    OpsProposalSubmission,
    OpsSessionRegistration,
    OpsTurnResponse,
    OpsTurnStatus,
)
from coire_ops.admin_client import AdminClient
from coire_ops.model import OpsModel

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("coire.ops")
meter = metrics.get_meter("coire.ops")
turns = meter.create_counter("coire_ops_turns_total", unit="1")
model_health_checks = meter.create_counter("coire_ops_model_health_checks_total", unit="1")


class OpsService:
    def __init__(
        self,
        *,
        admin: AdminClient,
        model: OpsModel,
        service_instance: str,
        heartbeat_s: float,
    ) -> None:
        self._admin = admin
        self._model = model
        self._service_instance = service_instance
        self._heartbeat_s = heartbeat_s
        self.session_id = uuid.uuid4()
        self.model_healthy = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._register_new_session()
        self.model_healthy = await self._model.healthy()
        model_health_checks.add(1, {"outcome": "healthy" if self.model_healthy else "degraded"})
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _register_new_session(self) -> None:
        self.session_id = uuid.uuid4()
        await self._admin.register_session(
            OpsSessionRegistration(
                session_id=self.session_id,
                service_instance=self._service_instance,
            )
        )
        logger.info(
            "ops session registered",
            extra={"ops_session_id": str(self.session_id)},
        )

    async def stop(self) -> None:
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._heartbeat_task
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_s)
            try:
                await self._admin.heartbeat_session(str(self.session_id))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    logger.exception(
                        "ops heartbeat request failed",
                        extra={"ops_session_id": str(self.session_id)},
                    )
                    continue
                logger.exception(
                    "ops session generation expired; registering a replacement",
                    extra={"ops_session_id": str(self.session_id)},
                )
                try:
                    await self._register_new_session()
                except Exception:
                    logger.exception("ops session re-registration failed")
                    continue
            except Exception:
                logger.exception(
                    "ops heartbeat request failed",
                    extra={"ops_session_id": str(self.session_id)},
                )
                continue
            try:
                self.model_healthy = await self._model.healthy()
            except Exception:
                self.model_healthy = False
                logger.exception("ops model health probe failed")
            model_health_checks.add(1, {"outcome": "healthy" if self.model_healthy else "degraded"})

    async def turn(self, *, conversation_id: uuid.UUID, question: str) -> OpsTurnResponse:
        with tracer.start_as_current_span("coire.ops.turn") as span:
            span.set_attribute("conversation_id", str(conversation_id))
            snapshot = await self._admin.read_snapshot()
            self.model_healthy = await self._model.healthy()
            if not self.model_healthy:
                turns.add(1, {"outcome": "degraded"})
                logger.warning(
                    "ops model unavailable",
                    extra={"conversation_id": str(conversation_id)},
                )
                raise RuntimeError("pinned ops model is unavailable")
            result = await self._model.run(question=question, snapshot=snapshot)
        issued = None
        status = OpsTurnStatus.ANSWERED
        if result.action is not None:
            issued = await self._admin.submit_proposal(
                OpsProposalSubmission(
                    conversation_id=conversation_id,
                    session_id=self.session_id,
                    action=result.action,
                    rationale=result.rationale or "Proposed by the isolated ops service.",
                )
            )
            status = OpsTurnStatus.PROPOSED
        turns.add(1, {"outcome": status.value})
        logger.info(
            "ops turn completed",
            extra={
                "conversation_id": str(conversation_id),
                "proposal_id": str(issued.proposal.id) if issued else None,
                "outcome": status.value,
            },
        )
        return OpsTurnResponse(
            status=status,
            answer=result.answer,
            observed_at=snapshot.observed_at,
            sources=["cluster.nodes", "cluster.instances", "alerts"],
            proposal=issued,
        )
