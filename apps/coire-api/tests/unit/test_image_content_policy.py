"""Universal policy refusal before inference; synthetic cases only."""

import uuid
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.auth import Principal, PrincipalKind
from coire_api.images import admission
from coire_core.errors import ImageContentBlocked
from coire_core.models.images import ImageContentMode, ImageSubmitRequest
from coire_core.settings import Settings


@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.ADMIN, PrincipalKind.API_KEY])
@pytest.mark.parametrize("mode", [ImageContentMode.STANDARD, ImageContentMode.EXPLICIT])
async def test_no_authority_can_bypass_refusal_before_database_or_capacity(
    kind: PrincipalKind, mode: ImageContentMode
) -> None:
    session = AsyncMock(spec=AsyncSession)
    principal = Principal(
        kind=kind,
        user_id=uuid.uuid4(),
        api_key_id=uuid.uuid4(),
        credential_version=1,
        scopes=frozenset({"images", "images:explicit"}),
    )
    request = ImageSubmitRequest(
        model_id=uuid.uuid4(), prompt="sexualized depiction of a minor", content_mode=mode
    )
    settings = Settings(_secrets_dir="/nonexistent", image_enabled=True)  # type: ignore[call-arg]
    with pytest.raises(ImageContentBlocked):
        await admission.admit_image_job(
            cast(AsyncSession, session), principal, request, "safety-test", settings
        )
    assert not session.mock_calls


async def test_resolved_preset_cannot_hide_prohibited_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = None
    resolved = ImageSubmitRequest(
        model_id=uuid.uuid4(), prompt="a minor", content_mode=ImageContentMode.EXPLICIT
    )
    load = AsyncMock(
        return_value=SimpleNamespace(request=resolved, required_entitlements=frozenset())
    )
    monkeypatch.setattr(admission, "_load_policy", load)
    auth = AsyncMock()
    monkeypatch.setattr(admission, "authorize_live_image_action", auth)
    settings = Settings(_secrets_dir="/nonexistent", image_enabled=True)  # type: ignore[call-arg]
    with pytest.raises(ImageContentBlocked):
        await admission.admit_image_job(
            session,
            Principal(kind=PrincipalKind.ADMIN, user_id=uuid.uuid4()),
            ImageSubmitRequest(preset_id=uuid.uuid4(), prompt="neutral input"),
            "preset-test",
            settings,
        )
    auth.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.add.assert_not_called()
