"""Terminal image jobs release owner inputs once, under exact row locks."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import ImageInputRow, ImageJobRow
from coire_api.images.input_references import release_image_input_references
from coire_core.errors import ImageConflict
from coire_core.models.images import ImageJobSettingsSnapshot, ImageMode, ImageSpec


async def test_terminal_reference_release_is_owner_bound_and_fenced() -> None:
    owner = uuid.uuid4()
    input_id = uuid.uuid4()
    spec = ImageSpec(
        model_id=uuid.uuid4(),
        mode=ImageMode.IMG2IMG,
        prompt="private subject",
        width=64,
        height=64,
        steps=4,
        guidance=Decimal(0),
        seed=1,
        init_image_id=input_id,
        strength=Decimal("0.375125"),
    )
    job = SimpleNamespace(
        owner_user_id=owner,
        resolved_spec=ImageJobSettingsSnapshot(effective_spec=spec).model_dump(mode="json"),
    )
    row = SimpleNamespace(owner_user_id=owner, active_references=1)

    class Session:
        async def get(self, model: type[object], identity: object, **kwargs: object) -> object:
            assert model is ImageInputRow and identity == input_id
            assert kwargs == {"populate_existing": True, "with_for_update": True}
            return row

    session = cast(AsyncSession, Session())
    row.owner_user_id = uuid.uuid4()
    with pytest.raises(ImageConflict, match="reference is unavailable"):
        await release_image_input_references(session, cast(ImageJobRow, job))
    assert row.active_references == 1
    row.owner_user_id = owner
    await release_image_input_references(session, cast(ImageJobRow, job))
    assert row.active_references == 0
    with pytest.raises(ImageConflict, match="reference is unavailable"):
        await release_image_input_references(session, cast(ImageJobRow, job))
