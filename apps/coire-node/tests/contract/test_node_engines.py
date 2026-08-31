"""Engine lifecycle against contracts/node-api.yaml (T045, T055).

These tests drive the fake engine (research R9), which reproduces the behaviour that shaped
the design: `/health` answers 200 while the model is still loading. A test that accepted
liveness as readiness would pass against the fake and fail against the real engine, so the
readiness assertions here check the *transition*, not just the end state.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

import psutil
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from coire_core.models.engine import EngineState, ReconcileExpectation, ReconcileRequest
from coire_node.engines import EngineManager, build_engine_argv
from coire_node.testing.harness import TOKEN, Agent

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "specs/001-model-registry-node-agent/contracts/node-api.yaml"
)
SLUG = "fake--tiny"


@pytest.fixture(scope="session")
def contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT.read_text())  # type: ignore[no-any-return]


def validator_for(contract: dict[str, Any], name: str) -> Draft202012Validator:
    schema = {**contract["components"]["schemas"][name], "$defs": contract["components"]["schemas"]}
    text = yaml.dump(schema).replace("#/components/schemas/", "#/$defs/")
    return Draft202012Validator(yaml.safe_load(text))


def seed(agent: Agent, slug: str = SLUG) -> None:
    """A verified copy, as a finished pull would leave it."""
    base = agent.store.path_for(slug)
    base.mkdir(parents=True, exist_ok=True)
    (base / "config.json").write_bytes(b"{}")
    (base / "model.safetensors").write_bytes(b"\x00" * 2048)
    agent.store.write_manifest(agent.store.hash_tree(slug, repo_id="fake/tiny", revision="r"))


def fake_engine_command(delay: float = 0.0, *, fail: bool = False, mb: int = 0) -> str:
    import sys

    parts = [sys.executable, "-m", "coire_node.testing.fake_engine"]
    if delay:
        parts += ["--load-delay", str(delay)]
    if fail:
        parts += ["--fail-on-start"]
    if mb:
        parts += ["--allocate-mb", str(mb)]
    return os.pathsep.join(parts)


def test_engine_argv_can_bind_loopback_only() -> None:
    argv = build_engine_argv(
        command=["python", "-m", "mlx_lm.server"],
        model_path="/opt/coire/models/example",
        host="127.0.0.1",
        port=9500,
    )
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert not any(value.endswith(".fabric") or value.endswith(".mesh") for value in argv)


def kill_stray_engines() -> None:
    """Kill every fake engine this test session started.

    Engines deliberately survive their agent — that is the whole of FR-015 — so a test that
    starts one leaks a process unless the test kills it. Left alone they accumulate and hold
    ports, and the next test's engine fails to bind.
    """
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except (psutil.Error, TypeError):
            continue
        if "coire_node.testing.fake_engine" not in cmdline:
            continue
        with contextlib.suppress(psutil.Error, ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.info["pid"]), signal.SIGKILL)


@pytest.fixture(autouse=True)
def _no_stray_engines() -> Any:
    """Every test in this module starts from, and leaves, a clean process table."""
    kill_stray_engines()
    yield
    kill_stray_engines()


@pytest.fixture
def engine_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("COIRE_ENGINE_COMMAND", fake_engine_command())
    agent = Agent(tmp_path / "node")
    seed(agent)
    yield agent
    agent.close()


def wait_state(agent: Agent, engine_id: uuid.UUID, *states: EngineState, timeout: float = 30.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = agent.engines.get(engine_id)
        if last is not None and last.state in states:
            return last
        time.sleep(0.1)
    raise AssertionError(f"engine did not reach {states}: {last}")


class TestStart:
    def test_ready_only_after_a_generation_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contract: dict[str, Any]
    ) -> None:
        """Spec FR-012. The engine's own /health answers long before this."""
        monkeypatch.setenv("COIRE_ENGINE_COMMAND", fake_engine_command(delay=4.0))
        agent = Agent(tmp_path / "node")
        seed(agent)
        try:
            engine_id = uuid.uuid4()
            created, status = agent.engines.start(
                engine_id=engine_id, slug=SLUG, estimate_bytes=1024
            )
            assert created is False  # newly started, not pre-existing
            assert status.state is EngineState.STARTING
            validator_for(contract, "EngineStatus").validate(status.model_dump(mode="json"))

            # Half way through the simulated load: the socket is up, so a liveness probe would
            # already say "ready". The agent must not.
            time.sleep(2.0)
            mid = agent.engines.get(engine_id)
            assert mid is not None and mid.state is EngineState.STARTING

            ready = wait_state(agent, engine_id, EngineState.READY)
            assert ready.pid and ready.process_create_time
            assert ready.load_seconds and ready.load_seconds >= 4.0
        finally:
            agent.close()

    def test_the_engine_runs_in_its_own_process_group(self, engine_agent: Agent) -> None:
        """Otherwise launchd's KeepAlive restart would kill it (research R4)."""
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        ready = wait_state(engine_agent, engine_id, EngineState.READY)
        assert ready.pid is not None
        assert os.getpgid(ready.pid) == ready.pid, "engine is not a process-group leader"

    def test_only_registry_resolved_paths_reach_the_engine(self, engine_agent: Agent) -> None:
        """Spec FR-017: no caller-supplied string is ever passed to an engine.

        Asserted against the pure argv builder rather than by patching the process layer:
        patching subprocess globally leaks into whatever runs next, and the claim is about the
        command line, which is exactly what this function returns.
        """
        from coire_node.engines import build_engine_argv, build_engine_env, engine_command

        store_path = str(engine_agent.store.path_for(SLUG))
        argv = build_engine_argv(
            command=engine_command(engine_agent.settings),
            model_path=store_path,
            host="192.168.100.11",
            port=9500,
        )
        assert argv[argv.index("--model") + 1] == store_path
        assert argv[argv.index("--host") + 1] == "192.168.100.11"
        # Every argument after the interpreter is a fixed flag, the store path, the bound
        # address, the port, or the log level. Nothing request-derived can appear.
        tail = argv[len(engine_command(engine_agent.settings)) :]
        assert tail == [
            "--model",
            store_path,
            "--host",
            "192.168.100.11",
            "--port",
            "9500",
            "--log-level",
            "INFO",
        ]

        env = build_engine_env({"HF_TOKEN": "hf_secret", "PATH": "/usr/bin"})
        assert env["HF_HUB_OFFLINE"] == "1", "an engine must not be able to fetch anything"
        assert "HF_TOKEN" not in env, "an engine has no business authenticating to the Hub"
        assert env["PATH"] == "/usr/bin"

    def test_a_chat_template_is_passed_as_a_file_never_inline(self, engine_agent: Agent) -> None:
        from coire_node.engines import build_engine_argv, engine_command

        template = "{{ messages[0].content }}"
        path = engine_agent.store.write_template(SLUG, template)
        argv = build_engine_argv(
            command=engine_command(engine_agent.settings),
            model_path=str(engine_agent.store.path_for(SLUG)),
            host="127.0.0.1",
            port=9500,
            chat_template_path=str(path),
        )
        assert argv[argv.index("--chat-template") + 1] == str(path)
        assert template not in " ".join(argv), "the template must not appear inline"
        assert path.read_text() == template
        # Beside the copy, so a template change never makes a verified copy look corrupt.
        manifest = engine_agent.store.read_manifest(SLUG)
        assert manifest is not None
        assert engine_agent.store.verify_against(SLUG, manifest) == []

    def test_the_started_engine_matches_the_built_command(self, engine_agent: Agent) -> None:
        """The builder is what start() actually uses — observed from outside the process."""
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        ready = wait_state(engine_agent, engine_id, EngineState.READY)
        assert ready.pid is not None
        cmdline = " ".join(psutil.Process(ready.pid).cmdline())
        assert str(engine_agent.store.path_for(SLUG)) in cmdline
        assert f"--port {ready.port}" in cmdline

    def test_authenticated_node_proxy_is_the_engine_network_boundary(
        self, engine_agent: Agent
    ) -> None:
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        wait_state(engine_agent, engine_id, EngineState.READY)
        payload = {
            "model": str(engine_agent.store.path_for(SLUG)),
            "messages": [{"role": "user", "content": "hello"}],
        }
        with TestClient(engine_agent.app()) as anonymous:
            assert (
                anonymous.post(
                    f"/node/engines/{engine_id}/proxy/v1/chat/completions", json=payload
                ).status_code
                == 401
            )
        with engine_agent.client() as gateway:
            response = gateway.post(
                f"/node/engines/{engine_id}/proxy/v1/chat/completions", json=payload
            )
        assert response.status_code == 200
        assert response.json()["choices"]

    def test_a_start_failure_reports_the_exit_status_and_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """US3 scenario 4: never ready, and the engine's own account of why."""
        monkeypatch.setenv("COIRE_ENGINE_COMMAND", fake_engine_command(fail=True))
        agent = Agent(tmp_path / "node")
        seed(agent)
        try:
            engine_id = uuid.uuid4()
            agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
            failed = wait_state(agent, engine_id, EngineState.FAILED)
            assert failed.exit_code == 3
            assert failed.exit_output and "Traceback" in failed.exit_output
            assert failed.state is not EngineState.READY
        finally:
            agent.close()

    def test_loading_without_a_verified_copy_is_refused(self, engine_agent: Agent) -> None:
        from coire_node.engines import CopyMissing

        with pytest.raises(CopyMissing):
            engine_agent.engines.start(
                engine_id=uuid.uuid4(), slug="absent--model", estimate_bytes=1
            )


class TestIdempotenceAndBudget:
    def test_a_second_load_returns_the_existing_engine(self, engine_agent: Agent) -> None:
        """Spec FR-019 — one process, not two."""
        first_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=first_id, slug=SLUG, estimate_bytes=1024)
        wait_state(engine_agent, first_id, EngineState.READY)

        already, status = engine_agent.engines.start(
            engine_id=uuid.uuid4(), slug=SLUG, estimate_bytes=1024
        )
        assert already is True
        assert status.engine_id == first_id
        live = [s for s in engine_agent.engines.statuses() if s.state is EngineState.READY]
        assert len(live) == 1

    def test_a_load_over_budget_is_refused_with_the_figures(self, engine_agent: Agent) -> None:
        """Spec FR-020 — "no" without numbers is not actionable."""
        from coire_node.engines import BudgetExceeded

        with pytest.raises(BudgetExceeded) as exc:
            engine_agent.engines.start(engine_id=uuid.uuid4(), slug=SLUG, estimate_bytes=10**15)
        refusal = exc.value.refusal
        assert refusal.required_bytes == 10**15
        assert refusal.budget_bytes > 0
        assert refusal.committed_bytes >= 0

    def test_concurrent_loads_cannot_both_consume_the_same_headroom(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec edge case 8: two loads that each fit but together do not."""
        import threading

        from coire_node.engines import BudgetExceeded

        monkeypatch.setenv("COIRE_ENGINE_COMMAND", fake_engine_command())
        agent = Agent(tmp_path / "node")
        seed(agent)
        seed(agent, "fake--second")
        try:
            budget = agent.engines.budget_bytes()
            each = int(budget * 0.6)  # two of these exceed the budget
            results: list[str] = []

            def attempt(slug: str) -> None:
                try:
                    agent.engines.start(engine_id=uuid.uuid4(), slug=slug, estimate_bytes=each)
                    results.append("admitted")
                except BudgetExceeded:
                    results.append("refused")

            threads = [
                threading.Thread(target=attempt, args=(SLUG,)),
                threading.Thread(target=attempt, args=("fake--second",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert sorted(results) == ["admitted", "refused"], results
        finally:
            agent.close()


class TestStopAndDeath:
    def test_unload_terminates_the_process(self, engine_agent: Agent) -> None:
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        ready = wait_state(engine_agent, engine_id, EngineState.READY)
        pid = ready.pid
        assert pid is not None

        engine_agent.engines.stop(engine_id)
        stopped = wait_state(engine_agent, engine_id, EngineState.STOPPED)
        assert stopped.state is EngineState.STOPPED
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        assert engine_agent.engines.committed_bytes() == 0

    def test_an_externally_killed_engine_is_noticed(self, engine_agent: Agent) -> None:
        """Spec SC-009 — within one health interval."""
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        ready = wait_state(engine_agent, engine_id, EngineState.READY)
        assert ready.pid is not None

        engine_agent.engines.start_health_loop()
        os.killpg(os.getpgid(ready.pid), signal.SIGKILL)
        started = time.monotonic()
        failed = wait_state(engine_agent, engine_id, EngineState.FAILED, timeout=10.0)
        elapsed = time.monotonic() - started
        assert failed.state_reason and "exited" in failed.state_reason
        # The configured interval is 0.5s in the harness; allow scheduling slack.
        assert elapsed < 5.0, f"took {elapsed:.1f}s to notice"

    def test_a_stopped_engine_frees_its_port_for_reuse(self, engine_agent: Agent) -> None:
        first = uuid.uuid4()
        engine_agent.engines.start(engine_id=first, slug=SLUG, estimate_bytes=1024)
        port = wait_state(engine_agent, first, EngineState.READY).port
        engine_agent.engines.stop(first)
        wait_state(engine_agent, first, EngineState.STOPPED)

        second = uuid.uuid4()
        _, status = engine_agent.engines.start(engine_id=second, slug=SLUG, estimate_bytes=1024)
        assert status.port == port


class TestReconcile:
    def test_a_new_manager_adopts_a_running_engine(
        self, engine_agent: Agent, contract: dict[str, Any]
    ) -> None:
        """Spec FR-015 — the agent restart case, simulated by a fresh manager on the same
        state directory."""
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        ready = wait_state(engine_agent, engine_id, EngineState.READY)
        engine_agent.engines.shutdown()

        fresh = EngineManager(engine_agent.settings, engine_agent.store, "127.0.0.1")
        adopted = fresh.adopt_from_state()
        assert [a.engine_id for a in adopted] == [engine_id]
        assert adopted[0].pid == ready.pid
        assert psutil.pid_exists(ready.pid or 0), "adoption must not have killed it"

        result = fresh.reconcile(
            ReconcileRequest(
                expected=[
                    ReconcileExpectation(
                        engine_id=engine_id,
                        slug=SLUG,
                        port=ready.port,
                        pid=ready.pid,
                        process_create_time=ready.process_create_time,
                    )
                ]
            )
        )
        validator_for(contract, "ReconcileResult").validate(result.model_dump(mode="json"))
        assert [a.engine_id for a in result.adopted] == [engine_id]
        assert result.dead == []
        fresh.shutdown()

    def test_an_engine_that_died_while_the_agent_was_down_is_reported_dead(
        self, engine_agent: Agent
    ) -> None:
        """US4 scenario 3."""
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        ready = wait_state(engine_agent, engine_id, EngineState.READY)
        engine_agent.engines.shutdown()
        os.killpg(os.getpgid(ready.pid or 0), signal.SIGKILL)
        time.sleep(0.5)

        fresh = EngineManager(engine_agent.settings, engine_agent.store, "127.0.0.1")
        assert fresh.adopt_from_state() == []
        result = fresh.reconcile(
            ReconcileRequest(
                expected=[
                    ReconcileExpectation(
                        engine_id=engine_id,
                        slug=SLUG,
                        port=ready.port,
                        pid=ready.pid,
                        process_create_time=ready.process_create_time,
                    )
                ]
            )
        )
        assert result.dead == [engine_id]
        assert result.adopted == []
        fresh.shutdown()

    def test_an_unexpected_engine_is_reported_as_an_orphan(self, engine_agent: Agent) -> None:
        """US4 scenario 2 — neither adopted nor killed."""
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        ready = wait_state(engine_agent, engine_id, EngineState.READY)

        result = engine_agent.engines.reconcile(ReconcileRequest(expected=[]))
        assert result.adopted == []
        assert any(o.pid == ready.pid for o in result.orphans)
        assert psutil.pid_exists(ready.pid or 0), "an orphan must not be killed"


class TestHealthSurface:
    def test_node_health_carries_engines_and_budget(self, engine_agent: Agent) -> None:
        """Spec FR-013: per-process CPU and resident memory, not just node totals."""
        engine_id = uuid.uuid4()
        engine_agent.engines.start(engine_id=engine_id, slug=SLUG, estimate_bytes=1024)
        wait_state(engine_agent, engine_id, EngineState.READY)

        with TestClient(engine_agent.app()) as client:
            body = client.get("/node/health", headers={"Authorization": f"Bearer {TOKEN}"}).json()

        assert body["memory_budget_bytes"] > 0
        assert body["memory_committed_bytes"] == 1024
        assert body["store_free_bytes"] > 0
        engines = body["engines"]
        assert len(engines) == 1
        assert engines[0]["resident_bytes"] and engines[0]["resident_bytes"] > 0
        assert engines[0]["resident_delta_bytes"] is not None
