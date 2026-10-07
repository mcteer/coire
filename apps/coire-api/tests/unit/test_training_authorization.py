"""Training privilege is user-bound, current and independent of broad service scopes."""

import uuid
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ApiKeyRow, UserRow
from coire_api.training.authorization import (
    authorize_live_training_action,
    preflight_training_action,
)
from coire_core.errors import TrainingForbidden
from coire_core.models.auth import UserRole


def admin() -> Principal:
    return Principal(
        kind=PrincipalKind.USER,
        user_id=uuid.uuid4(),
        role=UserRole.ADMIN,
        subject="admin@training.test",
    )


@pytest.mark.parametrize(
    "kind",
    [PrincipalKind.ANONYMOUS, PrincipalKind.RUN, PrincipalKind.SERVICE, PrincipalKind.OPS_SERVICE],
)
def test_service_admin_scope_does_not_grant_training_management(kind: PrincipalKind) -> None:
    principal = Principal(
        kind=kind, user_id=uuid.uuid4(), role=UserRole.ADMIN, scopes=frozenset({"admin"})
    )
    with pytest.raises(TrainingForbidden):
        preflight_training_action(principal, method="GET", origin=None, browser_origin="")


def test_legacy_admin_and_missing_browser_origin_are_refused() -> None:
    with pytest.raises(TrainingForbidden):
        preflight_training_action(
            Principal(kind=PrincipalKind.ADMIN), method="GET", origin=None, browser_origin=""
        )
    with pytest.raises(TrainingForbidden):
        preflight_training_action(
            admin(), method="POST", origin=None, browser_origin="https://coire.test"
        )
    principal = admin()
    assert (
        preflight_training_action(
            principal,
            method="POST",
            origin="https://coire.test",
            browser_origin="https://coire.test",
        )
        == principal.user_id
    )


class LiveSession:
    def __init__(self, user: UserRow, key: ApiKeyRow | None = None) -> None:
        self.user = user
        self.key = key

    async def get(
        self, model: type[object], identity: object, **kwargs: object
    ) -> UserRow | ApiKeyRow | None:
        assert kwargs.get("populate_existing") is True and kwargs.get("with_for_update") is True
        return self.user if model is UserRow else self.key


@pytest.mark.parametrize("active,role", [(False, UserRole.ADMIN), (True, UserRole.USER)])
async def test_live_revocation_overrides_stale_principal(active: bool, role: UserRole) -> None:
    principal = admin()
    session = LiveSession(UserRow(id=principal.user_id, active=active, role=role))
    with pytest.raises(TrainingForbidden):
        await authorize_live_training_action(cast(AsyncSession, session), principal)


async def test_live_key_rotation_scope_and_owner_are_rechecked() -> None:
    principal = admin().model_copy(
        update={
            "kind": PrincipalKind.API_KEY,
            "api_key_id": uuid.uuid4(),
            "credential_version": 2,
            "scopes": frozenset({"admin"}),
        }
    )
    user = UserRow(id=principal.user_id, active=True, role=UserRole.ADMIN)
    key = ApiKeyRow(
        id=principal.api_key_id,
        user_id=principal.user_id,
        credential_version=2,
        scopes=["admin"],
        revoked_at=None,
    )
    session = cast(AsyncSession, LiveSession(user, key))
    assert await authorize_live_training_action(session, principal) == principal.user_id
    changes: list[dict[str, object]] = [
        {"credential_version": 3},
        {"scopes": []},
        {"user_id": uuid.uuid4()},
    ]
    for change in changes:
        modified = ApiKeyRow(
            id=principal.api_key_id,
            user_id=principal.user_id,
            credential_version=2,
            scopes=["admin"],
            revoked_at=None,
        )
        for name, value in change.items():
            setattr(modified, name, value)
        with pytest.raises(TrainingForbidden):
            await authorize_live_training_action(
                cast(AsyncSession, LiveSession(user, modified)), principal
            )
