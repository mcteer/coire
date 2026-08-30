"""Fixtures for the node contract tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coire_core.models.node import NodePath
from coire_node.testing.harness import TOKEN, Agent


@pytest.fixture
def agent(tmp_path: Path) -> Iterator[Agent]:
    a = Agent(tmp_path / "node")
    yield a
    a.close()


@pytest.fixture
def client(agent: Agent) -> Iterator[TestClient]:
    with TestClient(agent.app(NodePath.MESH)) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c
