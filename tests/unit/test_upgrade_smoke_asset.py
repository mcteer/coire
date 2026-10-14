"""Smoke authority must not select arbitrarily among contradictory registry defaults."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.upgrades.service import smoke_asset
from coire_core.errors import UpgradeUnavailable
from coire_core.models.registry import ModelState


@pytest.mark.parametrize("count", (0, 2))
async def test_smoke_asset_refuses_missing_or_ambiguous_verified_default(count: int) -> None:
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = SimpleNamespace(
        state=ModelState.READY, source="studio", kind="language_model", backend="mlx_lm"
    )
    result = Mock()
    result.all.return_value = [SimpleNamespace(byte_size=1) for _ in range(count)]
    session.scalars.return_value = result
    with pytest.raises(UpgradeUnavailable, match="exactly one"):
        await smoke_asset(session, uuid4(), ("coire-edge-a",))
    session.execute.assert_not_called()
