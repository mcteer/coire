"""The executor credential authorizes only its exact fixed-operation role."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from coire_api.auth import Principal, PrincipalKind
from coire_api.upgrades.authorization import require_upgrade_executor
from coire_core.errors import UpgradeForbidden
from coire_core.models.auth import UserRole


def request() -> Request:
    app = FastAPI()
    app.state.settings = SimpleNamespace(upgrade_executor_id="executor-one")
    return Request(
        {"type": "http", "method": "POST", "path": "/internal", "headers": [], "app": app}
    )


async def test_exact_executor_role_and_subject() -> None:
    actor = Principal(
        kind=PrincipalKind.UPGRADE_EXECUTOR,
        subject="executor-one",
        scopes=frozenset({"upgrades:execute"}),
    )
    assert await require_upgrade_executor(request(), actor) == actor
    assert not actor.is_admin
    for invalid in (
        actor.model_copy(update={"subject": "executor-two"}),
        actor.model_copy(update={"scopes": frozenset({"upgrades:execute", "admin"})}),
        Principal(kind=PrincipalKind.OPS_SERVICE, subject="executor-one", scopes=actor.scopes),
        Principal(kind=PrincipalKind.ADMIN, user_id=uuid4(), role=UserRole.ADMIN),
    ):
        with pytest.raises(UpgradeForbidden):
            await require_upgrade_executor(request(), invalid)
