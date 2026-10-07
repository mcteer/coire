"""Verify fixture isolation and synthetic data without any model execution."""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from coire_core.models.node import NetworkPath
from coire_node.testing.harness import Agent


def test_synthetic_training_data(training_fixture_dir: Path) -> None:
    for name in ("text", "prompt_completion", "conversation", "tools"):
        rows = [
            json.loads(line)
            for line in (training_fixture_dir / f"{name}.jsonl").read_text().splitlines()
        ]
        assert rows and all(isinstance(row, dict) for row in rows)
    tools = json.loads((training_fixture_dir / "tools.jsonl").read_text())
    assert tools["messages"][1]["tool_calls"][0]["id"] == tools["messages"][2]["tool_call_id"]


def test_mirror_fixture_is_private_and_disjoint(training_replica_roots: tuple[Path, Path]) -> None:
    a, b = training_replica_roots
    assert a != b and a.parent == b.parent
    assert a.stat().st_mode & 0o777 == 0o700
    assert b.stat().st_mode & 0o777 == 0o700


def test_fake_node_is_local_authenticated_and_has_no_engines(training_fake_node: Agent) -> None:
    with training_fake_node.client(NetworkPath.CONTROL) as client:
        response = client.get("/node/health")
        assert response.status_code == 200
        assert response.json()["engines"] == []
        client.headers.pop("Authorization")
        assert client.get("/node/health").status_code == 401


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("COIRE_INTEGRATION") != "1",
    reason="set COIRE_INTEGRATION=1 to request the isolated Postgres gate",
)
async def test_training_database_is_disposable_postgres_17(training_postgres_url: str) -> None:
    engine = create_async_engine(training_postgres_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_database()")) == "training_test"
            assert str(await connection.scalar(text("SHOW server_version"))).startswith("17.")
    finally:
        await engine.dispose()
