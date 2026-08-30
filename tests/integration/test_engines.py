"""Engine lifecycle and adoption against the running stack (T048, T056).

The engine is the fake one (research R9) — MLX cannot run on Linux — but everything around it
is real: the control plane's load route, the node agent's process handling, the reconciler
mirroring state, and the audit trail. The real-engine equivalents run on the macOS job.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run engine tests",
    ),
]

PROJECT = "coire-it"
COMPOSE_DIR = "deploy/compose"
TEST_REPO = "mlx-community/Qwen2.5-0.5B-Instruct-4bit"


def _compose(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, *args],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _client(api_url: str) -> httpx.Client:
    return httpx.Client(base_url=api_url, timeout=30.0)


@pytest.fixture(scope="module")
def ready_model(api_url: str, admin_headers: dict[str, str]) -> dict[str, Any]:
    """The model acquired by test_acquisition, or acquired here if run alone."""
    with _client(api_url) as client:
        for model in client.get("/api/v1/admin/models", headers=admin_headers).json():
            if model["repo_id"] == TEST_REPO and model["state"] == "ready":
                return model
        resp = client.post(
            "/api/v1/admin/models", headers=admin_headers, json={"repo_id": TEST_REPO}
        )
        assert resp.status_code == 202, resp.text
        model_id = resp.json()["id"]
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            body = client.get(f"/api/v1/admin/models/{model_id}", headers=admin_headers).json()
            if body["state"] == "ready":
                return body
            assert body["state"] != "failed", body.get("state_reason")
            time.sleep(2)
    raise AssertionError("the model never became ready")


def wait_engine(
    api_url: str, headers: dict[str, str], engine_id: str, *states: str, timeout: float = 120.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    with _client(api_url) as client:
        while time.monotonic() < deadline:
            resp = client.get(f"/api/v1/admin/engines/{engine_id}", headers=headers)
            if resp.status_code == 200:
                last = resp.json()
                if last["state"] in states:
                    return last
            time.sleep(1.0)
    raise AssertionError(f"engine stayed at {last.get('state')}, wanted {states}")


def wait_nodes_healthy(api_url: str, headers: dict[str, str], *, timeout: float = 120.0) -> None:
    """Wait until every declared node is healthy again.

    Several tests here deliberately restart an agent. Loading onto a node that is still coming
    back produces a failure that looks like a defect in loading and is really a defect in the
    test's sequencing.
    """
    deadline = time.monotonic() + timeout
    last: list[dict[str, Any]] = []
    with _client(api_url) as client:
        while time.monotonic() < deadline:
            last = client.get("/api/v1/admin/nodes", headers=headers).json()
            if last and all(n["reachability"] == "healthy" for n in last):
                return
            time.sleep(2)
    raise AssertionError(f"nodes did not become healthy: {[n['reachability'] for n in last]}")


def unload_all(api_url: str, headers: dict[str, str], *, timeout: float = 90.0) -> None:
    """Stop every live engine and wait until each is genuinely terminal.

    Waiting matters: a sleep leaves engines in `stopping`, and the next test then loads
    against a node that is still tearing one down.
    """
    with _client(api_url) as client:
        engines = client.get("/api/v1/admin/engines", headers=headers).json()
        stopping = [e["id"] for e in engines if e["state"] in ("starting", "ready", "orphan")]
        for engine_id in stopping:
            client.delete(f"/api/v1/admin/engines/{engine_id}", headers=headers)

        deadline = time.monotonic() + timeout
        while stopping and time.monotonic() < deadline:
            current = client.get("/api/v1/admin/engines", headers=headers).json()
            by_id = {e["id"]: e for e in current}
            stopping = [
                e
                for e in stopping
                if by_id.get(e, {}).get("state") not in ("stopped", "failed", None)
            ]
            if stopping:
                time.sleep(2)


class TestLoadAndUnload:
    def test_a_model_loads_reports_and_unloads(
        self, api_url: str, admin_headers: dict[str, str], ready_model: dict[str, Any]
    ) -> None:
        wait_nodes_healthy(api_url, admin_headers)
        unload_all(api_url, admin_headers)
        with _client(api_url) as client:
            resp = client.post(
                f"/api/v1/admin/models/{ready_model['id']}/load", headers=admin_headers
            )
            assert resp.status_code == 202, resp.text
            engine = resp.json()
            assert engine["state"] == "starting"

            ready = wait_engine(api_url, admin_headers, engine["id"], "ready")
            assert ready["pid"]
            assert ready["port"] >= 9500
            # Spec FR-014 / SC-008: measured against the estimate, every load.
            assert ready["resident_bytes"] and ready["resident_bytes"] > 0
            assert ready["resident_delta_bytes"] is not None

            # The node reports it too, with per-process figures (spec FR-013). Select the
            # node actually hosting this engine: every node accumulates historical rows, so
            # "has engines" is not the same as "is running one".
            deadline = time.monotonic() + 30
            hosting = None
            while time.monotonic() < deadline:
                nodes = client.get("/api/v1/admin/nodes", headers=admin_headers).json()
                hosting = next(
                    (
                        n
                        for n in nodes
                        if any(e["id"] == engine["id"] for e in n["engines"]) and n.get("status")
                    ),
                    None,
                )
                if hosting and hosting["status"]["memory_committed_bytes"] > 0:
                    break
                time.sleep(2)
            assert hosting is not None, "no node reports this engine"
            assert hosting["status"]["memory_committed_bytes"] > 0
            live = next(e for e in hosting["engines"] if e["id"] == engine["id"])
            assert live["resident_bytes"] and live["resident_bytes"] > 0

            # FR-019: a second load is a no-op returning the same engine.
            again = client.post(
                f"/api/v1/admin/models/{ready_model['id']}/load", headers=admin_headers
            )
            assert again.status_code == 200
            assert again.json()["id"] == engine["id"]

            # Unload.
            stop = client.delete(f"/api/v1/admin/engines/{engine['id']}", headers=admin_headers)
            assert stop.status_code == 202
            stopped = wait_engine(api_url, admin_headers, engine["id"], "stopped", "failed")
            assert stopped["state"] == "stopped"

            audit = client.get(
                "/api/v1/admin/audit",
                headers=admin_headers,
                params={"target_id": engine["id"]},
            ).json()
            assert {"engine.load", "engine.unload"} <= {r["action"] for r in audit}

    def test_loading_a_model_that_is_not_ready_is_refused(
        self, api_url: str, admin_headers: dict[str, str]
    ) -> None:
        with _client(api_url) as client:
            models = client.get("/api/v1/admin/models", headers=admin_headers).json()
            not_ready = [m for m in models if m["state"] != "ready"]
            if not not_ready:
                pytest.skip("no non-ready model available")
            resp = client.post(
                f"/api/v1/admin/models/{not_ready[0]['id']}/load", headers=admin_headers
            )
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "not_ready"


class TestAgentRestartAdoption:
    """Spec SC-007 / FR-015: the engine survives, the registry keeps telling the truth."""

    def test_an_engine_survives_an_agent_restart_and_is_re_adopted(
        self, api_url: str, admin_headers: dict[str, str], ready_model: dict[str, Any]
    ) -> None:
        wait_nodes_healthy(api_url, admin_headers)
        unload_all(api_url, admin_headers)
        with _client(api_url) as client:
            engine = client.post(
                f"/api/v1/admin/models/{ready_model['id']}/load",
                headers=admin_headers,
                json={"node": "coire-edge-a"},
            ).json()
        ready = wait_engine(api_url, admin_headers, engine["id"], "ready")
        original_pid = ready["pid"]
        assert original_pid

        # Kill the agent, not the engine. The entrypoint loop restarts it, standing in for
        # launchd's KeepAlive.
        killed = _compose("exec", "-T", "node-a", "pkill", "-TERM", "-f", "coire_node$")
        assert killed.returncode in (0, 1), killed.stderr

        # The engine must still be running throughout.
        time.sleep(2)
        alive = _compose(
            "exec", "-T", "node-a", "sh", "-c", f"kill -0 {original_pid} && echo alive"
        )
        assert "alive" in alive.stdout, "the engine died with its agent — AbandonProcessGroup"

        # Wait for the agent to come back and re-register, which triggers a reconcile.
        deadline = time.monotonic() + 90
        adopted = None
        while time.monotonic() < deadline:
            with _client(api_url) as client:
                body = client.get(
                    f"/api/v1/admin/engines/{engine['id']}", headers=admin_headers
                ).json()
            if body["state"] == "ready" and body["pid"] == original_pid:
                adopted = body
                break
            time.sleep(2)

        assert adopted is not None, "the engine was not re-adopted after the agent restarted"
        assert adopted["pid"] == original_pid, "a different process was adopted"

        with _client(api_url) as client:
            audit = client.get(
                "/api/v1/admin/audit",
                headers=admin_headers,
                params={"target_id": engine["id"], "limit": 50},
            ).json()
        # Adoption is not a new load: nothing was started.
        assert sum(1 for r in audit if r["action"] == "engine.load") == 1

    def test_an_engine_that_dies_while_the_agent_is_down_is_corrected(
        self, api_url: str, admin_headers: dict[str, str], ready_model: dict[str, Any]
    ) -> None:
        """US4 scenario 3: the registry is corrected, and the discrepancy is recorded."""
        wait_nodes_healthy(api_url, admin_headers)
        unload_all(api_url, admin_headers)
        with _client(api_url) as client:
            engine = client.post(
                f"/api/v1/admin/models/{ready_model['id']}/load",
                headers=admin_headers,
                json={"node": "coire-edge-a"},
            ).json()
        ready = wait_engine(api_url, admin_headers, engine["id"], "ready")
        pid = ready["pid"]

        # Stop the agent, kill the engine underneath it, let the agent come back.
        _compose("exec", "-T", "node-a", "pkill", "-STOP", "-f", "coire_node$")
        _compose("exec", "-T", "node-a", "sh", "-c", f"kill -9 {pid}")
        _compose("exec", "-T", "node-a", "pkill", "-CONT", "-f", "coire_node$")
        _compose("exec", "-T", "node-a", "pkill", "-TERM", "-f", "coire_node$")

        deadline = time.monotonic() + 120
        final: dict[str, Any] = {}
        while time.monotonic() < deadline:
            with _client(api_url) as client:
                final = client.get(
                    f"/api/v1/admin/engines/{engine['id']}", headers=admin_headers
                ).json()
            if final["state"] in ("failed", "stopped"):
                break
            time.sleep(2)

        assert final["state"] in ("failed", "stopped"), final
        assert final["state_reason"], "a corrected row must say why"


class TestOrphans:
    def test_an_engine_nobody_started_is_reported_not_adopted_or_killed(
        self, api_url: str, admin_headers: dict[str, str], ready_model: dict[str, Any]
    ) -> None:
        """US4 scenario 2."""
        wait_nodes_healthy(api_url, admin_headers)
        unload_all(api_url, admin_headers)
        slug = TEST_REPO.replace("/", "--")
        started = _compose(
            "exec",
            "-T",
            "-d",
            "node-a",
            "/app/.venv/bin/python",
            "-m",
            "coire_node.testing.fake_engine",
            "--model",
            f"/opt/coire/models/{slug}",
            "--host",
            "192.168.100.11",
            "--port",
            "9599",
        )
        assert started.returncode == 0, started.stderr
        time.sleep(3)

        # The premise: a hand-started engine really is running. Without this a later failure
        # reads as "orphan detection is broken" when the engine simply never started.
        running = _compose(
            "exec",
            "-T",
            "node-a",
            "sh",
            "-c",
            "ps -eo args | grep -c '[f]ake_engine.*9599'",
        )
        assert running.stdout.strip() not in ("", "0"), (
            f"the hand-started engine is not running: {started.stdout}{started.stderr}"
        )

        # Force a reconcile by restarting the agent, which re-registers.
        _compose("exec", "-T", "node-a", "pkill", "-TERM", "-f", "coire_node$")

        deadline = time.monotonic() + 120
        orphan = None
        while time.monotonic() < deadline:
            with _client(api_url) as client:
                engines = client.get("/api/v1/admin/engines", headers=admin_headers).json()
            orphan = next((e for e in engines if e["state"] == "orphan"), None)
            if orphan:
                break
            time.sleep(3)

        assert orphan is not None, "a hand-started engine was not reported as an orphan"
        assert orphan["port"] == 9599

        # Neither adopted nor killed: it is still running.
        alive = _compose(
            "exec", "-T", "node-a", "sh", "-c", f"kill -0 {orphan['pid']} && echo alive"
        )
        assert "alive" in alive.stdout, "an orphan must not be killed"

        # An admin can clear it.
        with _client(api_url) as client:
            resp = client.delete(f"/api/v1/admin/engines/{orphan['id']}", headers=admin_headers)
        assert resp.status_code == 202
