"""Opt-in, production-isolated training fixtures; no engine runs on core."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from training_postgres import disposable_postgres

from coire_node.testing.harness import Agent


@pytest.fixture
def training_fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "training"


@pytest.fixture
def training_replica_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Two disposable artifact stores, without pretending they are physical Studios."""
    roots = (tmp_path / "replica-a", tmp_path / "replica-b")
    for root in roots:
        root.mkdir(mode=0o700)
    return roots


@pytest.fixture
def training_fake_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Agent]:
    """Reuse the node's in-process harness with fake inference, never a real Studio."""
    monkeypatch.setenv(
        "COIRE_ENGINE_COMMAND",
        os.pathsep.join([sys.executable, "-m", "coire_node.testing.fake_engine"]),
    )
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    agent = Agent(tmp_path / "training-node")
    try:
        yield agent
    finally:
        agent.close()


@pytest.fixture(scope="session")
def training_postgres_url() -> Iterator[str]:
    """Create a dedicated Postgres 17 container only when a test requests this fixture.

    Never discover/reuse an existing database, never publish beyond loopback, and never
    tear down another project's resources. Secrets are generated and not logged.
    """
    with disposable_postgres() as url:
        yield url
