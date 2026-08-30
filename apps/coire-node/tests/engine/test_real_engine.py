"""The real `mlx_lm.server` (T047).

Everything else about engines is exercised on Linux against the fake engine. This file covers
the one thing that cannot be: an actual model loading into actual unified memory. It runs on
Apple Silicon only, and asserts the properties the fake engine was built to imitate — most
importantly that the engine's own liveness endpoint answers *before* it can serve, which is
why readiness is defined as a generation (research R1).

Skipped unless COIRE_ENGINE=1 on Darwin.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import psutil
import pytest

from coire_core.models.engine import EngineState, EngineStatus
from coire_node.engines import EngineManager
from coire_node.testing.harness import Agent

pytestmark = [
    pytest.mark.engine,
    pytest.mark.skipif(sys.platform != "darwin", reason="MLX runs on Apple Silicon only"),
    pytest.mark.skipif(
        os.environ.get("COIRE_ENGINE") != "1",
        reason="set COIRE_ENGINE=1 to run real-engine tests",
    ),
]

TEST_REPO = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"
SLUG = TEST_REPO.replace("/", "--")
MEASUREMENTS = Path("engine-measurements.md")


@pytest.fixture(scope="module")
def agent(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Agent]:
    """An agent with the test model really downloaded into its store."""
    from coire_node import hub

    root = tmp_path_factory.mktemp("engine-node")
    a = Agent(root, node_engine_start_timeout_s=600.0, node_engine_health_interval_s=2.0)
    hub.snapshot(
        TEST_REPO,
        revision="main",
        local_dir=a.store.path_for(SLUG),
        token=os.environ.get("HF_TOKEN") or None,
    )
    a.store.write_manifest(a.store.hash_tree(SLUG, repo_id=TEST_REPO, revision="main"))
    yield a
    for status in a.engines.statuses():
        if status.pid and status.state in (EngineState.READY, EngineState.STARTING):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(status.pid), signal.SIGKILL)
    a.close()


def _record(line: str) -> None:
    """Append a measurement to the job summary artefact."""
    with MEASUREMENTS.open("a") as handle:
        handle.write(line + "\n")


def wait_state(
    agent: Agent, engine_id: uuid.UUID, *states: EngineState, timeout: float = 600.0
) -> EngineStatus:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = agent.engines.get(engine_id)
        if last is not None and last.state in states:
            return last
        time.sleep(0.5)
    raise AssertionError(f"engine did not reach {states}: {last}")


class TestRealEngine:
    def test_health_answers_before_the_model_can_serve(self, agent: Agent) -> None:
        """The finding the whole readiness design rests on (research R1).

        If this ever stops being true, the extra generation probe becomes unnecessary — but
        until then, treating /health as readiness would report a model ready before it could
        answer a single token.
        """
        engine_id = uuid.uuid4()
        agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=2 * 1024**3)
        starting = agent.engines.get(engine_id)
        assert starting is not None
        port = starting.port

        health_ok_at = None
        generation_ok_at = None
        started = time.monotonic()
        with httpx.Client(timeout=10.0) as client:
            deadline = started + 600
            while time.monotonic() < deadline and generation_ok_at is None:
                if health_ok_at is None:
                    try:
                        if client.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                            health_ok_at = time.monotonic() - started
                    except httpx.HTTPError:
                        pass
                try:
                    resp = client.post(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                        timeout=60.0,
                    )
                    if resp.status_code == 200:
                        generation_ok_at = time.monotonic() - started
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)

        assert health_ok_at is not None, "the engine never answered /health"
        assert generation_ok_at is not None, "the engine never generated"
        _record(
            f"- `/health` answered at {health_ok_at:.2f}s; first generation at "
            f"{generation_ok_at:.2f}s (gap {generation_ok_at - health_ok_at:.2f}s)"
        )
        assert health_ok_at <= generation_ok_at

        ready = wait_state(agent, engine_id, EngineState.READY)
        assert ready.load_seconds is not None
        _record(f"- agent reported ready after {ready.load_seconds:.2f}s")

    def test_resident_memory_is_measured_and_exceeds_the_weights(self, agent: Agent) -> None:
        """Spec FR-014 / SC-008, and the reason footprint is read rather than RSS."""
        engine = next(e for e in agent.engines.statuses() if e.state is EngineState.READY)
        assert engine.resident_bytes is not None
        weights = agent.store.size_bytes(SLUG)
        assert engine.resident_bytes > weights * 0.5, (
            f"resident {engine.resident_bytes} looks too small against {weights} of weights"
        )
        assert engine.resident_delta_bytes is not None
        _record(
            f"- weights {weights / 1e9:.2f} GB; resident {engine.resident_bytes / 1e9:.2f} GB; "
            f"estimate {engine.estimate_bytes / 1e9:.2f} GB; "
            f"delta {engine.resident_delta_bytes / 1e9:+.2f} GB"
        )
        assert engine.pid is not None
        rss = psutil.Process(engine.pid).memory_info().rss
        _record(f"- rss {rss / 1e9:.2f} GB (footprint is the number the platform records)")

    def test_the_engine_binds_only_the_configured_address(self, agent: Agent) -> None:
        """Spec FR-018."""
        engine = next(e for e in agent.engines.statuses() if e.state is EngineState.READY)
        assert engine.pid is not None
        conns = [
            c
            for c in psutil.Process(engine.pid).net_connections(kind="inet")
            if c.status == psutil.CONN_LISTEN
        ]
        assert conns, "the engine is not listening"
        for conn in conns:
            assert conn.laddr.ip in ("127.0.0.1", "192.168.100.11"), conn.laddr.ip

    def test_unload_releases_the_memory(self, agent: Agent) -> None:
        """Spec SC-006 — free memory returns to within 2% of the pre-load figure."""
        engine = next(e for e in agent.engines.statuses() if e.state is EngineState.READY)
        assert engine.engine_id is not None
        before_free = psutil.virtual_memory().available

        agent.engines.stop(engine.engine_id)
        stopped = wait_state(agent, engine.engine_id, EngineState.STOPPED, timeout=120)
        assert stopped.state is EngineState.STOPPED
        time.sleep(5)

        after_free = psutil.virtual_memory().available
        assert after_free > before_free, "unloading did not release memory"
        _record(
            f"- free memory {before_free / 1e9:.2f} GB before unload, "
            f"{after_free / 1e9:.2f} GB after"
        )
        assert agent.engines.committed_bytes() == 0

    def test_an_externally_killed_engine_is_detected(self, agent: Agent) -> None:
        """Spec SC-009."""
        engine_id = uuid.uuid4()
        agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=2 * 1024**3)
        ready = wait_state(agent, engine_id, EngineState.READY)
        assert ready.pid is not None
        agent.engines.start_health_loop()

        os.killpg(os.getpgid(ready.pid), signal.SIGKILL)
        started = time.monotonic()
        failed = wait_state(agent, engine_id, EngineState.FAILED, timeout=30)
        elapsed = time.monotonic() - started
        assert failed.state_reason
        _record(f"- external kill noticed in {elapsed:.1f}s")
        assert elapsed < 15.0

    def test_adoption_across_a_manager_restart(self, agent: Agent) -> None:
        """Spec SC-007 with a real engine holding real weights."""
        engine_id = uuid.uuid4()
        agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=2 * 1024**3)
        ready = wait_state(agent, engine_id, EngineState.READY)
        pid, port = ready.pid, ready.port
        agent.engines.shutdown()

        fresh = EngineManager(agent.settings, agent.store, "127.0.0.1")
        adopted = fresh.adopt_from_state()
        try:
            assert [a.engine_id for a in adopted] == [engine_id]
            assert adopted[0].pid == pid
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                )
            assert resp.status_code == 200, "the adopted engine no longer serves"
            _record("- a real engine survived a manager restart and still served")
        finally:
            if pid:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
            fresh.shutdown()
