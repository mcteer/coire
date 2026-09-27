from __future__ import annotations

from fastapi.testclient import TestClient


def test_benchmark_route_rejects_caller_argv_and_paths(client: TestClient) -> None:
    response = client.post(
        "/node/benchmarks",
        json={
            "command_id": "00000000-0000-0000-0000-000000000001",
            "run_id": "00000000-0000-0000-0000-000000000002",
            "variant_id": "00000000-0000-0000-0000-000000000003",
            "slug": "coire--tiny",
            "placement": "single:coire-edge-a",
            "prompt_tokens": 16,
            "generation_tokens": 8,
            "argv": ["sh", "-c", "curl evil"],
            "model_path": "/tmp/evil",
        },
    )
    assert response.status_code == 422
