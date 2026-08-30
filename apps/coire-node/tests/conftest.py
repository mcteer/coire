"""Shared node-agent fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from coire_node.testing.fake_hub import FakeHub


@pytest.fixture
def fake_hub(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[FakeHub]:  # type: ignore[no-untyped-def]
    """A local Hugging Face Hub.

    `HF_ENDPOINT` points the real client at it, so the client's own resolution, download and
    error paths run unmodified — including the exception classes the inspection code branches
    on, which is the whole point of a fake service rather than a mocked function.
    """
    with FakeHub() as hub:
        monkeypatch.setenv("HF_ENDPOINT", hub.endpoint)
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
        monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf" / "hub"))
        monkeypatch.setenv("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        # hf_xet talks to a CAS endpoint the fake Hub does not implement.
        monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
        monkeypatch.delenv("HF_TOKEN", raising=False)
        yield hub


@pytest.fixture
def engine_command(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Point the engine manager at the fake engine instead of mlx_lm.server."""
    import sys

    command = [sys.executable, "-m", "coire_node.testing.fake_engine"]
    monkeypatch.setenv("COIRE_ENGINE_COMMAND", os.pathsep.join(command))
    return command
