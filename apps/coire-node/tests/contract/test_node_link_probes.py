from __future__ import annotations

from fastapi.testclient import TestClient


def test_link_probe_rejects_caller_supplied_command_or_hosts(client: TestClient) -> None:
    response = client.post(
        "/node/link-probes",
        json={
            "command_id": "00000000-0000-0000-0000-000000000001",
            "transport": "jaccl",
            "hostfile_sha256": "0" * 64,
            "argv": ["sh", "-c", "curl evil"],
            "hosts": ["coire-core"],
        },
    )
    assert response.status_code == 422
