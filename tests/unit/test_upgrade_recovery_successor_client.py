"""Fixed successor transport scope and fresh explicit state, never fallback."""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
    UpgradeExpiredRecoverySuccessorClaim,
)
from coire_core.settings import Settings


@pytest.mark.parametrize("mode", ["check", "state", "claim"])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "pending",
        "foreign",
        "stale",
        "future",
        "redirect",
        "pending_expired",
        "accepted_after_ttl",
    ],
)
def test_successor_fixed_callback(tmp_path, monkeypatch, mode, fault):
    from coire_node.upgrade_guard import NativeGuardTransport

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    now = owner.claimed_at + timedelta(seconds=1)
    monkeypatch.setattr(
        "coire_node.upgrade_guard.datetime",
        SimpleNamespace(now=lambda zone: now + timedelta(milliseconds=1)),
    )
    proof = UpgradeExpiredRecoverySuccessionObservation(
        succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
        previous_owner=owner,
        successor=owner.process.model_copy(update={"pid": owner.process.pid + 1}),
        kernel_boot_identity=owner.kernel_boot_identity,
        preserved_source=owner.legacy_stop.preserved_source,
        observed_at=now,
        previous_owner_retired=True,
    )
    grant = UpgradeExpiredRecoverySuccessionGrant(
        observation=proof,
        user_id=owner.consumption.transfer.intent.accepted_by,
        key_id=None,
        credential_version=None,
        accepted_at=now,
        expires_at=min(now + timedelta(seconds=20), owner.consumption.transfer.intent.deadline_at),
    )
    claim = UpgradeExpiredRecoverySuccessorClaim(
        grant=grant,
        process=proof.successor,
        runtime=owner.runtime,
        kernel_boot_identity=owner.kernel_boot_identity,
        preserved_source=proof.preserved_source,
        claimed_at=now,
    )
    if fault in {"pending_expired", "accepted_after_ttl"}:
        now = grant.expires_at
    data = (
        claim.model_dump(mode="json")
        if mode in {"check", "claim"}
        else {
            "claim_sha256": claim.sha256(),
            "owner": None
            if fault in {"pending", "pending_expired"}
            else claim.model_dump(mode="json"),
            "checked_at": (
                now + timedelta(seconds=-6 if fault == "stale" else 60 if fault == "future" else 0)
            ).isoformat(),
        }
    )
    if fault == "foreign":
        if mode in {"check", "claim"}:
            data["process"]["pid"] += 1
        else:
            data["claim_sha256"] = "f" * 64
    api = None
    service = None
    if mode == "claim" and fault is None:
        from fastapi.testclient import TestClient

        from coire_api.app import create_app
        from coire_api.upgrades import expired_recovery_successor

        monkeypatch.setattr("coire_api.app.configure_telemetry", lambda *_: None)
        service = AsyncMock(return_value=(claim, True))
        monkeypatch.setattr(expired_recovery_successor, "accept_successor_claim", service)

        @asynccontextmanager
        async def session():
            yield object()

        monkeypatch.setattr("coire_api.routes.internal_upgrades.session_scope", session)
        api = TestClient(
            create_app(
                Settings(
                    upgrades_enabled=True, node_tokens=SecretStr('{"coire-edge-a":"fixture-token"}')
                )
            )
        )
        headers = {"X-Coire-Node": "coire-edge-a", "Authorization": "Bearer fixture-token"}
        path = f"/api/v1/internal/upgrades/{proof.succession_id}/recovery-successor-claim"
        for changed_headers in (
            {},
            {**headers, "Authorization": "Bearer wrong"},
            {**headers, "X-Coire-Node": "coire-edge-b"},
        ):
            assert (
                api.post(
                    path, json=claim.model_dump(mode="json"), headers=changed_headers
                ).status_code
                == 401
            )
        assert (
            api.post(
                path.replace(proof.succession_id, "01ARZ3NDEKTSV4RRFFQ69G5FAY"),
                json=claim.model_dump(mode="json"),
                headers=headers,
            ).status_code
            == 403
        )
        service.assert_not_awaited()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "core.private"
        assert (
            request.url.path
            == f"/api/v1/internal/upgrades/{proof.succession_id}/recovery-successor-{mode}"
        )
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert request.headers["X-Coire-Node"] == "coire-edge-a"
        assert json.loads(request.content) == (
            claim.model_dump(mode="json")
            if mode == "claim"
            else {
                "claim_sha256": claim.sha256(),
                "consumer": claim.process.model_dump(mode="json"),
                **({"grant_sha256": claim.grant.sha256()} if mode == "state" else {}),
            }
        )
        if api is not None:
            response = api.post(
                request.url.path, json=json.loads(request.content), headers=dict(request.headers)
            )
            assert response.headers["Cache-Control"] == "no-store"
            return httpx.Response(response.status_code, json=response.json())
        return httpx.Response(
            302 if fault == "redirect" else 200,
            json=data,
            headers={"Location": "http://foreign"} if fault == "redirect" else {},
        )

    settings = Settings(
        node_name="coire-edge-a",
        node_control_host="127.0.0.1",
        core_control_host="core.private",
        node_token=SecretStr("fixture-token"),
    )
    with httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    ) as client:
        control = NativeGuardTransport(settings, client)
        call = (
            control.accept_recovery_successor
            if mode == "claim"
            else control.verify_recovery_successor
            if mode == "check"
            else control.probe_recovery_successor
        )
        if (
            fault in {"foreign", "redirect"}
            or (mode == "claim" and fault in {"pending_expired", "accepted_after_ttl"})
            or (mode == "state" and fault in {"stale", "future", "pending_expired"})
        ):
            with pytest.raises(UpgradeUnavailable):
                call(claim)
        else:
            result = call(claim)
            assert (
                result == claim
                if mode in {"check", "claim"}
                else result.owner == (None if fault == "pending" else claim)
            )
    assert len(calls) == (
        0 if mode == "claim" and fault in {"pending_expired", "accepted_after_ttl"} else 1
    )
