"""Session lifecycle and model-backed turn orchestration for coire-ops."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

from coire_core.models.ops import (
    OpsProposalSubmission,
    OpsSessionRegistration,
    OpsTurnResponse,
    OpsTurnStatus,
)
from coire_ops.admin_client import AdminClient
from coire_ops.model import OpsModel


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
        await self._admin.register_session(
            OpsSessionRegistration(
                session_id=self.session_id,
                service_instance=self._service_instance,
            )
        )
        self.model_healthy = await self._model.healthy()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

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
            await self._admin.heartbeat_session(str(self.session_id))
            self.model_healthy = await self._model.healthy()

    async def turn(self, *, conversation_id: uuid.UUID, question: str) -> OpsTurnResponse:
        snapshot = await self._admin.read_snapshot()
        self.model_healthy = await self._model.healthy()
        if not self.model_healthy:
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
        return OpsTurnResponse(
            status=status,
            answer=result.answer,
            observed_at=snapshot.observed_at,
            sources=["cluster.nodes", "cluster.instances", "alerts"],
            proposal=issued,
        )
