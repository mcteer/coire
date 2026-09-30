"""Private image actions require a live user-bound principal."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.db import ImageOutputRow
from coire_api.images.authorization import (
    authorize_live_image_action,
    output_is_shareable,
    preflight_image_action,
    require_downloadable_image_output,
)
from coire_core.errors import ImageForbidden, ImageNotFound

OWNER = uuid.uuid4()
KEY_ID = uuid.uuid4()
ORIGIN = "https://coire.example.test"


def _user(kind: PrincipalKind = PrincipalKind.USER) -> Principal:
    return Principal(kind=kind, user_id=OWNER, subject=str(OWNER))


def _key(*scopes: str) -> Principal:
    return Principal(
        kind=PrincipalKind.API_KEY,
        user_id=OWNER,
        api_key_id=KEY_ID,
        credential_version=3,
        scopes=frozenset(scopes),
        entitlements=frozenset({"explicit"}),
    )


@pytest.mark.parametrize(
    "principal",
    [
        Principal(),
        Principal(kind=PrincipalKind.ADMIN),
        Principal(kind=PrincipalKind.RUN, user_id=OWNER),
        Principal(kind=PrincipalKind.OPS_SERVICE),
        Principal(kind=PrincipalKind.SERVICE, user_id=OWNER),
        _key("chat"),
    ],
)
def test_non_human_and_unscoped_credentials_fail(principal: Principal) -> None:
    with pytest.raises(ImageForbidden):
        preflight_image_action(principal, method="GET", origin=None, browser_origin=ORIGIN)


def test_browser_writes_require_exact_origin_but_reads_do_not() -> None:
    for kind in (PrincipalKind.USER, PrincipalKind.ADMIN):
        principal = _user(kind)
        assert (
            preflight_image_action(principal, method="GET", origin=None, browser_origin=ORIGIN)
            == OWNER
        )
        for bad_origin in (None, "https://evil.example.test", ORIGIN + "/", ORIGIN.upper()):
            with pytest.raises(ImageForbidden):
                preflight_image_action(
                    principal, method="POST", origin=bad_origin, browser_origin=ORIGIN
                )
        assert (
            preflight_image_action(principal, method="POST", origin=ORIGIN, browser_origin=ORIGIN)
            == OWNER
        )


def test_personal_key_requires_explicit_scope_for_explicit_action() -> None:
    assert (
        preflight_image_action(_key("images"), method="POST", origin=None, browser_origin=ORIGIN)
        == OWNER
    )
    with pytest.raises(ImageForbidden):
        preflight_image_action(
            _key("images"), method="POST", origin=None, browser_origin=ORIGIN, explicit=True
        )
    assert (
        preflight_image_action(
            _key("images", "images:explicit"),
            method="POST",
            origin=None,
            browser_origin=ORIGIN,
            explicit=True,
        )
        == OWNER
    )


class FakeScalars:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def all(self) -> list[str]:
        return self.names


class FakeSession:
    def __init__(
        self,
        *,
        active: bool = True,
        revoked: bool = False,
        version: int = 3,
        scopes: list[str] | None = None,
        entitlements: list[str] | None = None,
    ) -> None:
        self.user = SimpleNamespace(id=OWNER, active=active)
        self.key = SimpleNamespace(
            id=KEY_ID,
            user_id=OWNER,
            revoked_at=datetime.now(UTC) if revoked else None,
            credential_version=version,
            scopes=scopes if scopes is not None else ["images", "images:explicit"],
        )
        self.entitlements = entitlements if entitlements is not None else ["explicit"]

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object | None:
        assert kwargs == {"populate_existing": True, "with_for_update": True}
        if identity == OWNER:
            return self.user
        if identity == KEY_ID:
            return self.key
        return None

    async def scalars(self, statement: object) -> FakeScalars:
        return FakeScalars(self.entitlements)


async def test_live_revocation_and_key_rotation_override_cached_principal() -> None:
    principal = _key("images", "images:explicit")
    assert (
        await authorize_live_image_action(
            cast(AsyncSession, FakeSession()), principal, explicit=True
        )
        == OWNER
    )
    for session in (
        FakeSession(active=False),
        FakeSession(revoked=True),
        FakeSession(version=4),
        FakeSession(scopes=["chat"]),
        FakeSession(entitlements=[]),
    ):
        with pytest.raises(ImageForbidden):
            await authorize_live_image_action(cast(AsyncSession, session), principal, explicit=True)


async def test_live_dependency_entitlement_union_is_required() -> None:
    principal = _user()
    with pytest.raises(ImageForbidden):
        await authorize_live_image_action(
            cast(AsyncSession, FakeSession(entitlements=["explicit"])),
            principal,
            required_entitlements=frozenset({"control-pro"}),
        )
    assert (
        await authorize_live_image_action(
            cast(AsyncSession, FakeSession(entitlements=["control-pro"])),
            principal,
            required_entitlements=frozenset({"control-pro"}),
        )
        == OWNER
    )


async def test_live_owner_binding_and_human_explicit_entitlement() -> None:
    principal = _key("images", "images:explicit")
    mismatched = FakeSession()
    mismatched.key.user_id = uuid.uuid4()
    with pytest.raises(ImageForbidden):
        await authorize_live_image_action(cast(AsyncSession, mismatched), principal)
    with pytest.raises(ImageForbidden):
        await authorize_live_image_action(
            cast(AsyncSession, FakeSession(entitlements=[])), _user(), explicit=True
        )


def _output(*, tag: str = "normal", mode: str = "standard") -> ImageOutputRow:
    return cast(
        ImageOutputRow,
        SimpleNamespace(
            owner_user_id=OWNER,
            state="published",
            deleted_at=None,
            content_tag=tag,
            recipe={"resolved": {"spec": {"content_mode": mode}}},
        ),
    )


class FakeOutputSession(FakeSession):
    def __init__(
        self,
        output: ImageOutputRow | None,
        *,
        active: bool = True,
        revoked: bool = False,
        entitlements: list[str] | None = None,
    ) -> None:
        super().__init__(active=active, revoked=revoked, entitlements=entitlements)
        self.output = output

    async def get(self, model: type[object], identity: object, **kwargs: object) -> object | None:
        if identity not in (OWNER, KEY_ID):
            assert kwargs == {"populate_existing": True, "with_for_update": True}
            return self.output
        return await super().get(model, identity, **kwargs)


async def test_output_download_checks_owner_publication_and_live_identity() -> None:
    other_owner = _output()
    other_owner.owner_user_id = uuid.uuid4()
    staged = _output()
    staged.state = "staged"
    for row in (None, other_owner, staged):
        with pytest.raises(ImageNotFound):
            await require_downloadable_image_output(
                cast(AsyncSession, FakeOutputSession(row)), uuid.uuid4(), _user()
            )
    deleted = _output()
    deleted.deleted_at = datetime.now(UTC)
    with pytest.raises(ImageNotFound):
        await require_downloadable_image_output(
            cast(AsyncSession, FakeOutputSession(deleted)), uuid.uuid4(), _user()
        )
    with pytest.raises(ImageForbidden):
        await require_downloadable_image_output(
            cast(AsyncSession, FakeOutputSession(_output(), active=False)),
            uuid.uuid4(),
            _user(PrincipalKind.ADMIN),
        )


async def test_download_rechecks_policy_explicit_and_classifier_explicit() -> None:
    for row in (_output(tag="explicit"), _output(mode="explicit")):
        for principal, session in (
            (_user(PrincipalKind.ADMIN), FakeOutputSession(row, entitlements=[])),
            (_key("images"), FakeOutputSession(row)),
            (_key("images", "images:explicit"), FakeOutputSession(row, revoked=True)),
        ):
            with pytest.raises(ImageForbidden):
                await require_downloadable_image_output(
                    cast(AsyncSession, session), uuid.uuid4(), principal
                )
        assert (
            await require_downloadable_image_output(
                cast(AsyncSession, FakeOutputSession(row)),
                uuid.uuid4(),
                _key("images", "images:explicit"),
            )
            is row
        )


async def test_unknown_owner_access_and_shared_view_exclusion() -> None:
    unknown = _output(tag="unknown")
    assert (
        await require_downloadable_image_output(
            cast(AsyncSession, FakeOutputSession(unknown, entitlements=[])),
            uuid.uuid4(),
            _user(),
        )
        is unknown
    )
    assert output_is_shareable(_output())
    for row in (unknown, _output(tag="explicit"), _output(mode="explicit")):
        assert not output_is_shareable(row)


async def test_unrecognized_or_missing_policy_fails_closed() -> None:
    missing = _output()
    missing.recipe = {}
    malformed = _output()
    malformed.recipe = {"resolved": []}
    for row in (_output(tag="other"), _output(mode="other"), missing, malformed):
        assert not output_is_shareable(row)
        with pytest.raises(ImageForbidden):
            await require_downloadable_image_output(
                cast(AsyncSession, FakeOutputSession(row)), uuid.uuid4(), _user()
            )
