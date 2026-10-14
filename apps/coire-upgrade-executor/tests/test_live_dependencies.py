"""Dependency observations are fresh and bounded to the fixed Core graph."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import JsonValue
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeForbidden, UpgradeUnavailable
from coire_core.models.upgrades import CoreDependencyObservation
from coire_core.settings import Settings
from coire_upgrade_executor.docker import UpgradeDocker


@pytest.mark.parametrize("fault", [None, "stale", "future", "cycle", "foreign", "migration"])
async def test_live_dependencies_are_recollected_and_never_accept_an_unknown_graph(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str | None
) -> None:
    plan = core_plan(schema_changed=fault == "migration")
    observed_at = datetime.now(UTC) + timedelta(
        seconds=1 if fault == "future" else -6 if fault == "stale" else 0
    )

    def observation(
        role: Literal["postgres", "coire-api", "coire-migrate"],
        state: Literal["healthy", "completed"],
    ) -> CoreDependencyObservation:
        return CoreDependencyObservation(
            role=role,
            state=state,
            name=f"coire-{role}-1",
            project="coire",
            container_id="a" * 64,
            image_id="sha256:" + "b" * 64,
            configuration_sha256="c" * 64,
            observed_at=observed_at,
        )

    async with httpx.AsyncClient(trust_env=False) as http:
        docker = UpgradeDocker(Settings(), plan, client=http)
        postgres = AsyncMock(return_value=observation("postgres", "healthy"))
        migration = AsyncMock(return_value=observation("coire-migrate", "completed"))
        api = AsyncMock(return_value=observation("coire-api", "healthy"))
        monkeypatch.setattr(docker, "inspect_database_dependency", postgres)
        monkeypatch.setattr(docker, "inspect_migration_dependency", migration)
        monkeypatch.setattr(docker, "inspect_api_dependency", api)
        dependencies: dict[str, JsonValue] = {"postgres": {}, "coire-migrate": {}}
        configuration: dict[str, JsonValue] = {"depends_on": dependencies}
        if fault in {"cycle", "foreign"}:
            dependencies["coire-api" if fault == "cycle" else "arbitrary-service"] = {}
            with pytest.raises(UpgradeForbidden):
                await docker.fresh_dependencies("coire-api", configuration, tmp_path)
            postgres.assert_not_awaited()
            migration.assert_not_awaited()
        elif fault in {"stale", "future"}:
            with pytest.raises(UpgradeUnavailable):
                await docker.fresh_dependencies("coire-api", configuration, tmp_path)
        else:
            for _ in range(2):
                assert await docker.fresh_dependencies(
                    "coire-api", configuration, tmp_path, target=True
                ) == {"postgres": "healthy", "coire-migrate": "completed"}
            assert postgres.await_count == migration.await_count == 2
            assert migration.await_args is not None
            assert migration.await_args.kwargs["candidate"] == (fault == "migration")
            assert await docker.fresh_dependencies(
                "coire-web", {"depends_on": {"coire-api": {}}}, tmp_path
            ) == {"coire-api": "healthy"}
            api.assert_awaited_once()
