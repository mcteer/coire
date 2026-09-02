from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from conftest import drain_runtime, wait_nodes_healthy

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy/compose"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="set COIRE_INTEGRATION=1 to run sharding tests",
    ),
]


def _sql_value(statement: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "coire-it",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "coire",
            "-d",
            "coire",
            "-Atc",
            statement,
        ],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _candidate(
    client: httpx.Client, headers: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    for model in client.get("/api/v1/admin/models", headers=headers).json():
        variants = client.get(
            f"/api/v1/admin/models/{model['id']}/variants", headers=headers
        ).json()
        for variant in variants:
            if variant["validated"] and variant["state"] == "ready":
                return model, variant
    raise AssertionError("acquisition scenarios must provide a verified model")


def _fallback_candidate(
    client: httpx.Client, headers: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    for model in client.get("/api/v1/admin/models", headers=headers).json():
        variants = [
            item
            for item in client.get(
                f"/api/v1/admin/models/{model['id']}/variants", headers=headers
            ).json()
            if item["validated"] and item["state"] == "ready"
        ]
        variants.sort(key=lambda item: int(item["memory_estimate_bytes"]), reverse=True)
        if len(variants) >= 2 and int(variants[0]["memory_estimate_bytes"]) > int(
            variants[-1]["memory_estimate_bytes"]
        ):
            return model, variants[0]
    raise AssertionError("acquisition scenarios must provide a smaller fallback variant")


def _wait_instance(
    client: httpx.Client, instance_id: str, headers: dict[str, str], states: set[str]
) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/instances/{instance_id}", headers=headers).json()
        if body["state"] in states:
            return cast(dict[str, Any], body)
        time.sleep(0.5)
    raise AssertionError(f"instance {instance_id} did not reach {states}")


def _open_link(client: httpx.Client, headers: dict[str, str]) -> None:
    for _ in range(3):
        response = client.post(
            "/api/v1/admin/links/studios/probe", headers=headers, json={"force": True}
        )
        assert response.status_code == 202, response.text
    assert response.json()["tp_eligible"] is True


def _create_tp(
    client: httpx.Client, headers: dict[str, str], model: dict[str, Any], variant: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/instances",
        headers=headers,
        json={"model_id": model["id"], "variant_id": variant["id"], "policy": "sharded:tp"},
    )
    assert response.status_code == 202, response.text
    ready = _wait_instance(client, response.json()["id"], headers, {"ready", "failed"})
    assert ready["state"] == "ready", json.dumps(ready, indent=2)
    assert [member["rank"] for member in ready["members"]] == [0, 1]
    assert len({member["reservation_id"] for member in ready["members"]}) == 2
    return ready


def _create_tp_failure(
    client: httpx.Client, headers: dict[str, str], model: dict[str, Any], variant: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/instances",
        headers=headers,
        json={"model_id": model["id"], "variant_id": variant["id"], "policy": "sharded:tp"},
    )
    assert response.status_code == 202, response.text
    failed = _wait_instance(client, response.json()["id"], headers, {"ready", "failed"})
    assert failed["state"] == "failed", failed
    return failed


def _create_single(
    client: httpx.Client,
    headers: dict[str, str],
    model: dict[str, Any],
    variant: dict[str, Any],
    node: str,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/instances",
        headers=headers,
        json={
            "model_id": model["id"],
            "variant_id": variant["id"],
            "policy": f"single:{node}",
        },
    )
    assert response.status_code == 202, response.text
    ready = _wait_instance(client, response.json()["id"], headers, {"ready", "failed"})
    assert ready["state"] == "ready", json.dumps(ready, indent=2)
    return ready


def _ensure_single(
    client: httpx.Client,
    headers: dict[str, str],
    model: dict[str, Any],
    variant: dict[str, Any],
    node: str,
) -> dict[str, Any]:
    instances = client.get("/api/v1/instances", headers=headers).json()
    resident = next(
        (
            item
            for item in instances
            if item["model_id"] == model["id"]
            and item["state"] == "ready"
            and item["policy"] == f"single:{node}"
        ),
        None,
    )
    return cast(
        dict[str, Any],
        resident or _create_single(client, headers, model, variant, node),
    )


def test_synthetic_over_250gb_admission_serves_through_gateway(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=120) as client:
        drain_runtime(client, admin_headers)
        wait_nodes_healthy(client, admin_headers)
        model, variant = _candidate(client, admin_headers)
        _open_link(client, admin_headers)
        synthetic_bytes = 270 * 1024**3
        required = (synthetic_bytes + 1) // 2
        ledgers = client.get("/api/v1/admin/ledger", headers=admin_headers).json()
        original_budgets = {item["node_id"]: item["budget_bytes"] for item in ledgers}
        _sql_value(
            f"UPDATE model_variants SET memory_estimate_bytes={synthetic_bytes} "
            f"WHERE id='{variant['id']}'; "
            "UPDATE node_memory_ledgers ledger SET budget_bytes="
            "COALESCE((SELECT sum(bytes) FROM memory_reservations reservation "
            "WHERE reservation.node_id=ledger.node_id AND reservation.state IN "
            "('pending','held','releasing')),0)+"
            f"{required}+1073741824; SELECT 1"
        )
        try:
            group = _create_tp(client, admin_headers, model, variant)
            completion = client.post(
                "/v1/chat/completions",
                headers=admin_headers,
                json={"model": model["id"], "messages": [{"role": "user", "content": "large"}]},
            )
            assert completion.status_code == 200, completion.text
            assert completion.json()["choices"][0]["message"]["content"] == "ok"
            drained = client.delete(f"/api/v1/instances/{group['id']}", headers=admin_headers)
            assert drained.status_code == 202, drained.text
            _wait_instance(client, group["id"], admin_headers, {"stopped"})
        finally:
            _sql_value(
                f"UPDATE model_variants SET memory_estimate_bytes={variant['memory_estimate_bytes']} "
                f"WHERE id='{variant['id']}'; "
                + "; ".join(
                    f"UPDATE node_memory_ledgers SET budget_bytes={budget} "
                    f"WHERE node_id='{node_id}'"
                    for node_id, budget in original_budgets.items()
                )
                + "; SELECT 1"
            )


def test_probe_two_rank_gateway_drain_benchmark_and_rank_failure(
    api_url: str, admin_headers: dict[str, str], request: pytest.FixtureRequest
) -> None:
    with httpx.Client(base_url=api_url, timeout=120) as client:
        drain_runtime(client, admin_headers)
        model, variant = _candidate(client, admin_headers)
        ledgers = client.get("/api/v1/admin/ledger", headers=admin_headers).json()
        original_budgets = {str(item["node_id"]): int(item["budget_bytes"]) for item in ledgers}
        request.addfinalizer(
            lambda: _sql_value(
                "; ".join(
                    f"UPDATE node_memory_ledgers SET budget_bytes={budget} "
                    f"WHERE node_id='{node_id}'"
                    for node_id, budget in original_budgets.items()
                )
                + "; SELECT 1"
            )
        )
        required_per_node = (int(variant["memory_estimate_bytes"]) + 1) // 2
        for ledger in ledgers:
            minimum = int(ledger["reserved_bytes"]) + required_per_node + 1024**3
            if int(ledger["budget_bytes"]) < minimum:
                changed = client.patch(
                    f"/api/v1/admin/ledger/{ledger['node_id']}",
                    headers=admin_headers,
                    json={"budget_bytes": minimum},
                )
                assert changed.status_code == 200, changed.text
        _sql_value("DELETE FROM link_observations")
        unmeasured = _create_tp_failure(client, admin_headers, model, variant)
        assert unmeasured["failure_code"] == "rdma_probe_required"

        _open_link(client, admin_headers)
        _sql_value(
            "WITH updated AS (UPDATE link_observations SET latency_ms=5000 RETURNING 1) "
            "SELECT count(*) FROM updated"
        )
        projection = client.get("/api/v1/admin/links/studios", headers=admin_headers).json()
        assert projection["tp_eligible"] is True
        ready = _create_tp(client, admin_headers, model, variant)
        subprocess.run(
            ["docker", "compose", "-p", "coire-it", "restart", "coire-api", "coire-scheduler"],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                recovered = client.get(f"/api/v1/instances/{ready['id']}", headers=admin_headers)
                if recovered.status_code == 200 and recovered.json()["state"] == "ready":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise AssertionError("ready sharded group did not survive control-plane restart")
        completion = client.post(
            "/v1/chat/completions",
            headers=admin_headers,
            json={"model": model["id"], "messages": [{"role": "user", "content": "sharded"}]},
        )
        assert completion.status_code == 200, completion.text
        assert completion.json()["choices"][0]["message"]["content"] == "ok"

        drained = client.delete(f"/api/v1/instances/{ready['id']}", headers=admin_headers)
        assert drained.status_code == 202, drained.text
        stopped = _wait_instance(client, ready["id"], admin_headers, {"stopped", "failed"})
        assert stopped["state"] == "stopped", stopped
        assert (
            _sql_value(
                "SELECT count(*) FROM memory_reservations "
                f"WHERE holder_id='{ready['id']}' AND state!='released'"
            )
            == "0"
        )

        _sql_value(
            "INSERT INTO link_observations "
            "(id,node_a_id,node_b_id,transport,outcome,os_version_a,os_version_b,"
            "engine_version,reason,observed_at) "
            "SELECT gen_random_uuid(),"
            "(SELECT id FROM nodes WHERE name='coire-edge-a'),"
            "(SELECT id FROM nodes WHERE name='coire-edge-b'),"
            "'jaccl'::probe_transport,'failed'::probe_outcome,'ci-linux','ci-linux',"
            "'fake-1','forced integration failure',"
            "now()-make_interval(secs=>2-n) "
            "FROM generate_series(1,2) n RETURNING id"
        )
        down = client.get("/api/v1/admin/links/studios", headers=admin_headers).json()
        assert down["tp_eligible"] is False
        gated = _create_tp_failure(client, admin_headers, model, variant)
        assert gated["failure_code"] == "rdma_probe_required"
        single_during_outage = _ensure_single(client, admin_headers, model, variant, "coire-edge-a")
        single_completion = client.post(
            "/v1/chat/completions",
            headers=admin_headers,
            json={"model": model["id"], "messages": [{"role": "user", "content": "single"}]},
        )
        assert single_completion.status_code == 200, single_completion.text
        assert single_during_outage["state"] == "ready"
        _open_link(client, admin_headers)

        for node_name in ("coire-edge-a", "coire-edge-b"):
            _ensure_single(client, admin_headers, model, variant, node_name)
        ledgers = client.get("/api/v1/admin/ledger", headers=admin_headers).json()
        original_budgets = {item["node_id"]: item["budget_bytes"] for item in ledgers}
        half = (int(variant["memory_estimate_bytes"]) + 1) // 2
        for ledger in ledgers:
            changed = client.patch(
                f"/api/v1/admin/ledger/{ledger['node_id']}",
                headers=admin_headers,
                json={"budget_bytes": int(ledger["reserved_bytes"]) + half - 1},
            )
            assert changed.status_code == 200, changed.text
        evicting = _create_tp(client, admin_headers, model, variant)
        decision = client.get(
            f"/api/v1/admin/placements/{evicting['placement_decision_id']}",
            headers=admin_headers,
        )
        assert decision.status_code == 200, decision.text
        assert len(decision.json()["evicted_reservation_ids"]) == 2
        victim_ids = _sql_value(
            "SELECT holder_id FROM memory_reservations WHERE id IN ("
            + ",".join(
                f"'{reservation_id}'"
                for reservation_id in decision.json()["evicted_reservation_ids"]
            )
            + ") ORDER BY holder_id"
        ).splitlines()
        assert len(victim_ids) == 2
        for victim_id in victim_ids:
            stopped_single = _wait_instance(client, victim_id, admin_headers, {"stopped", "failed"})
            assert stopped_single["state"] == "stopped", stopped_single
        drained_eviction = client.delete(
            f"/api/v1/instances/{evicting['id']}", headers=admin_headers
        )
        assert drained_eviction.status_code == 202, drained_eviction.text
        _wait_instance(client, evicting["id"], admin_headers, {"stopped"})
        for node_id, budget in original_budgets.items():
            restored = client.patch(
                f"/api/v1/admin/ledger/{node_id}",
                headers=admin_headers,
                json={"budget_bytes": budget},
            )
            assert restored.status_code == 200, restored.text

        benchmark = client.post(
            "/api/v1/admin/benchmarks",
            headers=admin_headers,
            json={"variant_id": variant["id"], "prompt_tokens": 16, "generation_tokens": 8},
        )
        assert benchmark.status_code == 202, benchmark.text
        run_id = benchmark.json()["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            runs = client.get("/api/v1/admin/benchmarks", headers=admin_headers).json()
            run = next(item for item in runs if item["id"] == run_id)
            if run["state"] in {"completed", "failed"}:
                break
            time.sleep(0.5)
        assert run["state"] == "completed", run
        assert [result["placement"] for result in run["results"]] == [
            "single:coire-edge-a",
            "sharded:tp",
            "sharded:pp",
        ]
        assert all(result["tokens_per_second"] > 0 for result in run["results"])

        failed_group = _create_tp(client, admin_headers, model, variant)
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "coire-it",
                "exec",
                "-T",
                "node-a",
                "pkill",
                "-f",
                "fake_engine.*--port 9600",
            ],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
        )
        failed = _wait_instance(client, failed_group["id"], admin_headers, {"failed"})
        assert failed["failure_code"] == "rank_lost"
        assert failed["fallback_attempted_at"] is not None
        assert failed["fallback_instance_id"] is None
        assert failed["fallback_no_fit"] is True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            remaining = _sql_value(
                "SELECT count(*) FROM memory_reservations "
                f"WHERE holder_id='{failed_group['id']}' AND state!='released'"
            )
            if remaining == "0":
                break
            time.sleep(0.5)
        assert remaining == "0"
        state = client.get("/api/v1/state", headers=admin_headers).json()
        edge_a = next(node for node in state["nodes"] if node["name"] == "coire-edge-a")
        assert edge_a["reachability"] == "degraded"
        # The rank-loss assertion above deliberately degrades node-a. Recover the node
        # control state before exercising the independent single-node outage path; the
        # simulated inter-Studio link failure remains in place below.
        _sql_value(
            "UPDATE nodes SET reachability='healthy'::reachability "
            "WHERE name IN ('coire-edge-a','coire-edge-b'); "
            "UPDATE node_memory_ledgers SET health='healthy'::reachability,health_reason=NULL "
            "WHERE node_id IN (SELECT id FROM nodes WHERE name IN "
            "('coire-edge-a','coire-edge-b')); SELECT 1"
        )


def test_rank_failure_creates_one_smaller_survivor_fallback(
    api_url: str, admin_headers: dict[str, str]
) -> None:
    with httpx.Client(base_url=api_url, timeout=120) as client:
        # The preceding no-fit scenario deliberately leaves the failed rank degraded. Model
        # an operator-confirmed recovery after the healthy node endpoint is back before testing
        # the independent bounded-fallback branch.
        _sql_value(
            "UPDATE nodes SET reachability='healthy'::reachability "
            "WHERE name IN ('coire-edge-a','coire-edge-b'); "
            "UPDATE node_memory_ledgers SET health='healthy'::reachability,health_reason=NULL "
            "WHERE node_id IN (SELECT id FROM nodes WHERE name IN "
            "('coire-edge-a','coire-edge-b')); SELECT 1"
        )
        deadline = time.monotonic() + 30
        recovered: set[str] = set()
        while time.monotonic() < deadline:
            state = client.get("/api/v1/state", headers=admin_headers).json()
            recovered = {
                node["name"] for node in state["nodes"] if node["reachability"] == "healthy"
            }
            if recovered >= {"coire-edge-a", "coire-edge-b"}:
                break
            time.sleep(0.5)
        assert recovered >= {"coire-edge-a", "coire-edge-b"}
        model, largest = _fallback_candidate(client, admin_headers)
        _open_link(client, admin_headers)
        group = _create_tp(client, admin_headers, model, largest)
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "coire-it",
                "exec",
                "-T",
                "node-a",
                "pkill",
                "-f",
                "fake_engine.*--port 9600",
            ],
            cwd=COMPOSE_DIR,
            check=True,
            capture_output=True,
        )
        failed = _wait_instance(client, group["id"], admin_headers, {"failed"})
        assert failed["fallback_attempted_at"] is not None
        assert failed["fallback_no_fit"] is False
        assert failed["fallback_instance_id"] is not None
        fallback = _wait_instance(
            client, failed["fallback_instance_id"], admin_headers, {"ready", "failed"}
        )
        assert fallback["state"] == "ready", fallback
        assert fallback["policy"] == "single:coire-edge-b"
        assert fallback["variant_id"] != largest["id"]
        # Reconciliation is bounded: subsequent passes retain the same fallback identity.
        time.sleep(3)
        again = client.get(f"/api/v1/instances/{group['id']}", headers=admin_headers).json()
        assert again["fallback_instance_id"] == failed["fallback_instance_id"]
