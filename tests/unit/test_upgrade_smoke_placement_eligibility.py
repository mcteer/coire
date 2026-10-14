"""A private Core smoke never pins to an unqualified image/chat mixture."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from upgrade_fixtures import core_plan

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ModelInstanceRow
from coire_api.upgrades import smoke_instance
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import UpgradeSmokeRequest
from coire_core.settings import Settings


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("eligible", [(False, True), (False, False), (True, True)])
async def test_private_smoke_selects_a_copy_allowed_by_the_existing_image_gate(
    monkeypatch: pytest.MonkeyPatch, restored: bool, eligible: tuple[bool, bool]
) -> None:
    plan = core_plan()
    nodes = [
        SimpleNamespace(
            id=uuid4(),
            name=name,
            runtime_sha256=digest * 64,
            health_observed_at=datetime.now(UTC),
        )
        for name, digest in (("coire-edge-a", "a"), ("coire-edge-b", "b"))
    ]
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = None
    session.scalars.return_value = SimpleNamespace(all=lambda: nodes)
    added: list[ModelInstanceRow] = []
    session.add = added.append

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, session)

    gate = AsyncMock(side_effect=eligible)
    asset = AsyncMock(
        return_value=(
            plan.model_id,
            plan.model_variant_id,
            plan.model_slug,
            plan.model_manifest_sha256,
        )
    )
    monkeypatch.setattr(smoke_instance, "session_scope", scope)
    monkeypatch.setattr(smoke_instance, "scoped_operation", AsyncMock(return_value=(None, plan)))
    monkeypatch.setattr(smoke_instance, "smoke_asset", asset)
    monkeypatch.setattr(smoke_instance, "append_initial_transition", AsyncMock())
    monkeypatch.setattr("coire_scheduler.image_admission.new_chat_mix_allowed", gate)
    principal = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR, subject="coire", scopes=frozenset({"upgrades:execute"})
    )
    deadline = datetime.now(UTC) + timedelta(seconds=60)
    request = UpgradeSmokeRequest(
        generation=plan.operation.generation, check="source" if restored else "gateway"
    )
    if not any(eligible):
        with pytest.raises(UpgradeUnavailable, match="fresh verified tiny Studio copy"):
            await smoke_instance.prepare_smoke_instance(
                plan, request, principal, Settings(), deadline
            )
        assert not added
        asset.assert_not_awaited()
    else:
        _, selected = await smoke_instance.prepare_smoke_instance(
            plan, request, principal, Settings(), deadline
        )
        expected = nodes[eligible.index(True)]
        assert selected.node == expected.name
        assert selected.runtime_sha256 == expected.runtime_sha256
        assert selected.deadline_at == deadline
        assert selected.plan == plan
        assert len(added) == 1 and added[0].policy == "pinned:" + expected.name
        asset.assert_awaited_once_with(session, plan.model_id, (expected.name,))
    assert gate.await_args_list[0].args[:3] == (session, nodes[0].id, plan.model_variant_id)
