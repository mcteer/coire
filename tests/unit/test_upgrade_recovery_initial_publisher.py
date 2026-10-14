"""Fixed initial producer never follows redirects or renews a physical sample."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from test_upgrade_recovery_initial_relay import observation

from coire_core.errors import UpgradeUnavailable
from coire_core.settings import Settings
from coire_node.upgrade_guard import NativeGuardTransport


@pytest.mark.parametrize("fault", [None, "stale", "future", "foreign", "redirect", "lost", "late"])
def test_initial_publisher_exact_scope_and_original_sample_budget(tmp_path, monkeypatch, fault):
    proof = observation(tmp_path)
    now = [proof.observed_at]
    if fault == "stale":
        now[0] += timedelta(seconds=6)
    elif fault == "future":
        now[0] -= timedelta(seconds=1)
    monkeypatch.setattr(
        "coire_node.upgrade_guard.datetime", SimpleNamespace(now=lambda zone: now[0])
    )
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "core.private"
        assert (
            request.url.path
            == f"/api/v1/internal/upgrades/{proof.request.original_job_id}/recovery-initial-observation"
        )
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if fault == "lost":
            raise httpx.ReadError("fixture lost response", request=request)
        body = proof.model_dump(mode="json")
        if fault == "foreign":
            body["request"]["idempotency_key"] = "foreign"
        if fault == "late":
            now[0] += timedelta(seconds=6)
        return httpx.Response(
            302 if fault == "redirect" else 200,
            json=body,
            headers={"Location": "http://foreign.invalid"} if fault == "redirect" else {},
        )

    settings = Settings(
        node_name="coire-edge-a",
        node_control_host="127.0.0.1",
        core_control_host="core.private",
        node_token=SecretStr("fixture-token"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False
    ) as client:
        transport = NativeGuardTransport(settings, client)
        if fault:
            with pytest.raises(UpgradeUnavailable):
                transport.publish_initial_recovery_observation(proof)
        else:
            assert transport.publish_initial_recovery_observation(proof) == proof
    assert len(calls) == (0 if fault in {"stale", "future"} else 1)
