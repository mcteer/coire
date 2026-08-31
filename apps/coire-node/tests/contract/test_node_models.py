"""Feature 022 export-listener isolation contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from coire_core.models.node import NetworkPath
from coire_node.testing.harness import Agent

GRANT = "d" * 44


def test_transfer_grant_is_usable_only_on_data_listener(agent: Agent) -> None:
    slug = "fake--data-only"
    path = agent.store.path_for(slug)
    path.mkdir(parents=True)
    (path / "config.json").write_text("{}")
    agent.store.write_manifest(agent.store.hash_tree(slug, repo_id="fake/data", revision="r"))
    agent.grants.register(GRANT, slug, datetime.now(UTC) + timedelta(minutes=5))

    with TestClient(agent.app(NetworkPath.CONTROL)) as control:
        assert control.get(f"/node/export/{GRANT}/manifest").status_code == 404
    with TestClient(agent.app(NetworkPath.DATA)) as data:
        assert data.get(f"/node/export/{GRANT}/manifest").status_code == 200
