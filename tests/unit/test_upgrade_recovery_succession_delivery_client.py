"""Fixed passive grant read cannot renew epochs or follow caller destinations."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import (
    UpgradeExpiredRecoveryOwnerClaim,
    UpgradeExpiredRecoverySuccessionGrant,
    UpgradeExpiredRecoverySuccessionObservation,
)
from coire_core.settings import Settings


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "foreign",
        "stale",
        "future",
        "redirect",
        "expired",
        "granted",
        "grant_expired",
        "grant_foreign",
    ],
)
def test_fixed_delivery_read(tmp_path, monkeypatch, fault):
    from coire_node.upgrade_guard import NativeGuardTransport

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    intent = owner.consumption.transfer.intent
    now = intent.deadline_at if fault == "expired" else owner.claimed_at + timedelta(seconds=1)
    monkeypatch.setattr("coire_node.upgrade_guard.datetime", SimpleNamespace(now=lambda zone: now))
    if fault == "grant_expired":
        now = owner.claimed_at + timedelta(seconds=10)
    successor = owner.process.model_copy(update={"pid": owner.process.pid + 1})
    grant = None
    if fault in {"granted", "grant_expired", "grant_foreign"}:
        epoch = now - timedelta(seconds=3) if fault == "grant_expired" else now
        grant = UpgradeExpiredRecoverySuccessionGrant(
            observation=UpgradeExpiredRecoverySuccessionObservation(
                succession_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
                previous_owner=owner,
                successor=successor.model_copy(update={"pid": successor.pid + 1})
                if fault == "grant_foreign"
                else successor,
                kernel_boot_identity=owner.kernel_boot_identity,
                preserved_source=owner.legacy_stop.preserved_source,
                observed_at=epoch,
                previous_owner_retired=True,
            ),
            user_id=intent.accepted_by,
            key_id=None,
            credential_version=None,
            accepted_at=epoch,
            expires_at=epoch + timedelta(seconds=2),
        )
    calls = []

    def handler(request):
        import json

        calls.append(request)
        assert (
            request.url.host == "core.private"
            and request.url.path
            == f"/api/v1/internal/upgrades/{intent.recovery_id}/recovery-succession-delivery"
        )
        assert (
            request.headers["Authorization"] == "Bearer fixture-token"
            and request.headers["X-Coire-Node"] == "coire-edge-a"
        )
        body = json.loads(request.content)
        assert body["predecessor_sha256"] == owner.sha256() and body[
            "successor"
        ] == successor.model_dump(mode="json")
        if fault == "foreign":
            body["succession_id"] = "01ARZ3NDEKTSV4RRFFQ69G5FAY"
        return httpx.Response(
            302 if fault == "redirect" else 200,
            json={
                "request": body,
                "grant": grant.model_dump(mode="json") if grant is not None else None,
                "checked_at": (
                    now
                    + timedelta(
                        seconds=-6
                        if fault == "stale"
                        else -2
                        if fault == "grant_expired"
                        else 1
                        if fault == "future"
                        else 0
                    )
                ).isoformat(),
            },
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
        if fault and fault != "granted":
            with pytest.raises(UpgradeUnavailable):
                control.read_recovery_succession(owner, "01ARZ3NDEKTSV4RRFFQ69G5FAX", successor)
        else:
            state = control.read_recovery_succession(owner, "01ARZ3NDEKTSV4RRFFQ69G5FAX", successor)
            assert state.grant == grant
    assert len(calls) == (0 if fault == "expired" else 1)
