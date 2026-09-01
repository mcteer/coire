from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from coire_ops.model import OPS_TOOL_NAMES, OpsModel, OpsModelTurn
from coire_ops.service import OpsService

from coire_core.models.console import ConsoleCapabilities, ConsoleSnapshot
from coire_core.models.ops import OpsProposalIssued, OpsSession, OpsSessionState


def _snapshot() -> ConsoleSnapshot:
    return ConsoleSnapshot(
        observed_at=datetime.now(UTC),
        cursor="1",
        capabilities=ConsoleCapabilities(),
        cluster={
            "observed_at": datetime.now(UTC),
            "nodes": [],
            "instances": [],
            "studio_link": None,
        },
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
