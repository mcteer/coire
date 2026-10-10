"""Scheduler transport to the real gateway's frozen measurement boundary."""

from __future__ import annotations

import uuid

import httpx
from opentelemetry import trace
from opentelemetry.propagate import inject
from sqlalchemy import select

from coire_api.auth import Principal
from coire_api.db import InstanceMemberRow, NodeRow, session_scope
from coire_api.training.service import payload_digest
from coire_api.training.telemetry import observed
from coire_core.errors import TrainingConflict
from coire_core.models.training import (
    TrainingMeasurementCompletion,
    TrainingMeasurementGenerateRequest,
    TrainingMeasurementPrompt,
    TrainingResidentTarget,
)
from coire_core.settings import Settings


class MeasurementGatewayClient:
    """Credentials come from the existing node-token map; no user secret is copied."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.training_input_api_url.rstrip("/"), trust_env=False, timeout=60
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    # A sustained qualification exceeds the backend's per-trace bound if every
    # stream shares one parent. Keep each request bounded and link its origin.
    @observed("coire.scheduler.training.measurement.gateway", linked_root=True)
    async def generate(
        self,
        principal: Principal,
        measurement_id: uuid.UUID,
        target: TrainingResidentTarget,
        prompt: TrainingMeasurementPrompt,
        max_output_tokens: int,
    ) -> TrainingMeasurementCompletion:
        span = trace.get_current_span()
        span.set_attribute("measurement_id", str(measurement_id))
        span.set_attribute("instance_id", str(target.instance_id))
        async with session_scope() as session:
            node = (
                await session.execute(
                    select(NodeRow.name)
                    .join(InstanceMemberRow, InstanceMemberRow.node_id == NodeRow.id)
                    .where(InstanceMemberRow.instance_id == target.instance_id)
                )
            ).scalar_one_or_none()
        token = self.settings.node_token_map.get(node or "")
        if node is None or not token:
            raise TrainingConflict("Measurement gateway node binding is unavailable")
        body = TrainingMeasurementGenerateRequest(
            principal_sha256=payload_digest(principal),
            target=target,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
        )
        headers = {"Authorization": "Bearer " + token, "X-Coire-Node": node}
        inject(headers)
        try:
            response = await self.client.post(
                f"/api/v1/internal/training/measurements/{measurement_id}/generate",
                json=body.model_dump(mode="json"),
                headers=headers,
            )
            response.raise_for_status()
            result = TrainingMeasurementCompletion.model_validate_json(response.content)
        except (httpx.HTTPError, ValueError):
            raise TrainingConflict("Measurement gateway generation was refused") from None
        if result.instance_id != target.instance_id or result.target != target.target:
            raise TrainingConflict("Measurement gateway returned a different resident")
        return result
