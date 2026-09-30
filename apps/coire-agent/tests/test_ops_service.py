from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from coire_ops.model import (
    OPS_ANTHROPIC_MODEL,
    OPS_TOOL_NAMES,
    OpsModel,
    OpsModelTurn,
    compact_snapshot,
)
from coire_ops.service import OpsService

from coire_core.models.console import ConsoleCapabilities, ConsoleSnapshot
from coire_core.models.instance import ClusterState, InstanceState, ModelInstance
from coire_core.models.ops import OpsProposalIssued, OpsSession, OpsSessionState


def _snapshot() -> ConsoleSnapshot:
    return ConsoleSnapshot(
        observed_at=datetime.now(UTC),
        cursor="1",
        capabilities=ConsoleCapabilities(),
        cluster=ClusterState(observed_at=datetime.now(UTC), nodes=[], instances=[]),
        ledgers=[],
        alerts=[],
    )


class FakeAdmin:
    def __init__(self) -> None:
        self.registered = False
        self.submission = None

    async def register_session(self, registration):  # type: ignore[no-untyped-def]
        self.registered = True
        now = datetime.now(UTC)
        return OpsSession(
            id=registration.session_id,
            service_instance=registration.service_instance,
            state=OpsSessionState.ACTIVE,
            started_at=now,
            last_seen_at=now,
        )

    async def heartbeat_session(self, session_id):  # type: ignore[no-untyped-def]
        raise AssertionError("heartbeat should not run in this test")

    async def read_snapshot(self) -> ConsoleSnapshot:
        return _snapshot()

    async def submit_proposal(self, submission):  # type: ignore[no-untyped-def]
        self.submission = submission
        return cast(OpsProposalIssued, object())


class FakeModel:
    healthy_now = True

    async def healthy(self) -> bool:
        return self.healthy_now

    async def run(self, *, question: str, snapshot: ConsoleSnapshot) -> OpsModelTurn:
        return OpsModelTurn(answer=f"Observed: {question}")


@pytest.mark.asyncio
async def test_service_tracks_model_health_and_recovers_without_restart() -> None:
    admin = FakeAdmin()
    model = FakeModel()
    service = OpsService(
        admin=cast(object, admin),  # type: ignore[arg-type]
        model=cast(object, model),  # type: ignore[arg-type]
        service_instance="ops-test",
        heartbeat_s=3600,
    )
    await service.start()
    assert admin.registered and service.model_healthy
    model.healthy_now = False
    with pytest.raises(RuntimeError, match="unavailable"):
        await service.turn(conversation_id=uuid.uuid4(), question="status")
    model.healthy_now = True
    response = await service.turn(conversation_id=uuid.uuid4(), question="status")
    assert response.answer == "Observed: status"
    assert service.model_healthy
    await service.stop()


def test_model_toolset_is_exactly_bounded_read_and_propose() -> None:
    assert {"read_snapshot", "propose_reversible_action"} == OPS_TOOL_NAMES
    forbidden = {"confirm", "shell", "filesystem", "git", "docker", "delete", "retire"}
    assert not any(fragment in tool for fragment in forbidden for tool in OPS_TOOL_NAMES)
    assert not hasattr(OpsModel, "confirm")


def test_ops_snapshot_keeps_current_instances_without_historical_payload() -> None:
    snapshot = _snapshot()
    now = datetime.now(UTC)
    instances = [
        ModelInstance(
            id=uuid.uuid4(),
            model_id=uuid.uuid4(),
            variant_id=uuid.uuid4(),
            policy="single:auto",
            state=state,
            effective_state=state,
            in_flight=0,
            created_at=now,
            updated_at=now,
            transitioned_at=now,
            failure_detail="historical details " * 100 if state is InstanceState.FAILED else None,
        )
        for state in [InstanceState.FAILED] * 50 + [InstanceState.READY, InstanceState.WARMING]
    ]
    snapshot.cluster.instances = instances
    result = compact_snapshot(snapshot)
    assert result["instance_counts"] == {"failed": 50, "ready": 1, "warming": 1}
    active = cast(list[dict[str, object]], result["active_instances"])
    assert len(active) == 2
    assert {row["id"] for row in active} == {str(instances[-1].id), str(instances[-2].id)}
    assert len(str(result).encode()) < 3_000


@pytest.mark.asyncio
async def test_pinned_sonnet_ops_model_health_uses_only_scoped_api_relay() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": OPS_ANTHROPIC_MODEL})

    model = OpsModel(
        gateway_url="http://coire-api:8000/v1",
        token="ops-service",
        model_id=OPS_ANTHROPIC_MODEL,
        model_source="anthropic",
        ops_api_url="http://coire-api:8000",
        transport=httpx.MockTransport(respond),
    )
    assert model.tool_names == OPS_TOOL_NAMES
    assert await model.healthy()
    assert len(requests) == 1
    assert (
        str(requests[0].url)
        == f"http://coire-api:8000/api/v1/internal/ops/anthropic/v1/models/{OPS_ANTHROPIC_MODEL}"
    )
    assert requests[0].headers["x-api-key"] == "ops-service"
    with pytest.raises(ValueError, match="pinned Sonnet"):
        OpsModel(
            gateway_url="http://coire-api:8000/v1",
            token="ops-service",
            model_id="claude-other",
            model_source="anthropic",
        )


@pytest.mark.asyncio
async def test_failed_heartbeat_registers_a_fresh_session_generation() -> None:
    class RecoveringAdmin(FakeAdmin):
        def __init__(self) -> None:
            super().__init__()
            self.registrations: list[uuid.UUID] = []
            self.heartbeat_attempted = asyncio.Event()

        async def register_session(self, registration):  # type: ignore[no-untyped-def]
            self.registrations.append(registration.session_id)
            return await super().register_session(registration)  # type: ignore[no-untyped-call]

        async def heartbeat_session(self, session_id):  # type: ignore[no-untyped-def]
            self.heartbeat_attempted.set()
            request = httpx.Request("PATCH", f"https://coire.test/{session_id}")
            response = httpx.Response(409, request=request)
            raise httpx.HTTPStatusError("stale", request=request, response=response)

    admin = RecoveringAdmin()
    service = OpsService(
        admin=cast(object, admin),  # type: ignore[arg-type]
        model=cast(object, FakeModel()),  # type: ignore[arg-type]
        service_instance="ops-test",
        heartbeat_s=0.01,
    )
    await service.start()
    first = service.session_id
    await asyncio.wait_for(admin.heartbeat_attempted.wait(), timeout=1)
    for _ in range(100):
        if len(admin.registrations) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(admin.registrations) >= 2
    assert service.session_id != first
    await service.stop()
