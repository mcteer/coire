from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import AgentRunRow, RunCommandRow
from coire_api.routes.admin_runs import kill_run
from coire_core.models.auth import UserRole
from coire_core.models.runs import AgentRunState, RunKillRequest


class Session:
    def __init__(self, row: AgentRunRow) -> None:
        self.row = row
        self.committed = False
        self.added: list[object] = []

    async def get(self, model: object, identifier: uuid.UUID) -> AgentRunRow | None:
        return self.row if identifier == self.row.id else None

    async def commit(self) -> None:
        self.committed = True

    def add(self, value: object) -> None:
        self.added.append(value)


async def test_admin_kill_revokes_before_transition_and_audits(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    admin_id = uuid.uuid4()
    now = datetime.now(UTC)
    row = AgentRunRow(
        id=uuid.uuid4(),
        requester_user_id=uuid.uuid4(),
        profile="general",
        primary_model_id=uuid.uuid4(),
        primary_variant_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        workspace_ref="workspace",
        token_scope={},
        state=AgentRunState.RUNNING,
        limits={},
        resource_usage={},
        requested_at=now,
        updated_at=now,
    )
    session = Session(row)
    events: list[str] = []

    async def revoke(*_: object) -> None:
        events.append("revoke")

    async def transition(_session, run, state, reason):  # type: ignore[no-untyped-def]
        events.append("transition")
        run.state = state

    async def audit(*_: object, **__: object) -> None:
        events.append("audit")

    monkeypatch.setattr("coire_api.routes.admin_runs.revoke_run_token", revoke)
    monkeypatch.setattr("coire_api.routes.admin_runs.runs.transition", transition)
    monkeypatch.setattr("coire_api.routes.admin_runs.write_principal_audit", audit)
    principal = Principal(
        kind=PrincipalKind.ADMIN,
        subject=str(admin_id),
        user_id=admin_id,
        role=UserRole.ADMIN,
    )
    result = await kill_run(
        row.id,
        RunKillRequest(reason="stop"),
        principal,
        cast(AsyncSession, session),
    )
    assert events == ["revoke", "transition", "audit"]
    assert result.state is AgentRunState.KILL_REQUESTED
    assert result.killed_by == admin_id
    assert session.committed
    command = next(item for item in session.added if isinstance(item, RunCommandRow))
    assert command.operation.value == "kill"
