"""Audited inhibit and break-glass overrides. Both expire and both require the core key."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, HTTPException, status

from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin
from coire_api.deps import SessionDep, SettingsDep
from coire_api.failover.overrides import OverrideRejected, get_override_store
from coire_core.failover_crypto import public_key_b64
from coire_core.models.audit import AuditOutcome
from coire_core.models.failover import FailoverOverride

router = APIRouter(prefix="/api/v1/admin/failover", tags=["admin: failover"])


@router.post("/overrides", status_code=status.HTTP_204_NO_CONTENT)
async def apply_override(
    body: FailoverOverride,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> None:
    private_key = settings.failover_peer_key.get_secret_value().strip()
    if not private_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "failover signing key is not configured"
        )
    try:
        get_override_store().apply(body, public_key_b64(private_key))
    except (OverrideRejected, ValueError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "override rejected") from exc
    async with httpx.AsyncClient() as client:

        async def deliver(name: str) -> bool:
            try:
                response = await client.post(
                    f"http://{name}:{settings.node_listen_port}/node/failover/election/override",
                    json=body.model_dump(mode="json"),
                    timeout=3.0,
                )
                return response.status_code == status.HTTP_204_NO_CONTENT
            except httpx.HTTPError:
                return False

        names = ("coire-edge-a", "coire-edge-b")
        results = await asyncio.gather(*(deliver(name) for name in names))
    delivered = [name for name, ok in zip(names, results, strict=True) if ok]
    await write_principal_audit(
        session,
        principal=principal,
        action=f"failover.override.{body.kind.value}",
        target_type="failover_override",
        target_id=body.kind.value,
        outcome=AuditOutcome.OK,
        detail={
            "reason": body.reason,
            "expires_at": body.expires_at.isoformat(),
            "delivered_to": delivered,
        },
    )
    await session.commit()
    if len(delivered) != len(names):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "override delivery incomplete")
