"""Human-bound override intent is committed before one-host delivery; no broadcast authority."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from coire_api.audit import write_principal_audit
from coire_api.auth import CurrentAdmin, PrincipalKind
from coire_api.db import AuditRow
from coire_api.deps import SessionDep, SettingsDep
from coire_core.failover_crypto import public_key_b64, verify_ed25519
from coire_core.failover_poller import peer_url
from coire_core.models.audit import AuditOutcome
from coire_core.models.failover import FailoverOverride, FailoverOverrideAcknowledgement

router = APIRouter(prefix="/api/v1/admin/failover", tags=["admin: failover"])


@router.post("/overrides", response_model=FailoverOverrideAcknowledgement)
async def apply_override(
    body: FailoverOverride,
    principal: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
) -> FailoverOverrideAcknowledgement:
    private_key = settings.failover_peer_key.get_secret_value().strip()
    if not private_key:
        raise HTTPException(503, "failover signing key is not configured")
    valid = (
        principal.kind is PrincipalKind.ADMIN
        and principal.user_id == body.actor_id
        and body.epoch == settings.failover_membership_epoch
        and body.is_current()
        and verify_ed25519(body.canonical_bytes(), body.signature, public_key_b64(private_key))
    )
    if not valid:
        await write_principal_audit(
            session,
            principal=principal,
            action="failover.override.refused",
            target_type="failover_override",
            target_id=str(body.override_id),
            outcome=AuditOutcome.REFUSED,
            detail={"target_host": body.target_host, "reason": "actor_or_authority_invalid"},
        )
        await session.commit()
        raise HTTPException(403, "override rejected")
    previous = await session.scalar(
        select(AuditRow.id)
        .where(
            AuditRow.action == "failover.override.intent",
            AuditRow.target_type == "failover_override",
            AuditRow.target_id == str(body.override_id),
        )
        .limit(1)
    )
    if previous is not None:
        raise HTTPException(403, "override replay refused")
    await write_principal_audit(
        session,
        principal=principal,
        action="failover.override.intent",
        target_type="failover_override",
        target_id=str(body.override_id),
        outcome=AuditOutcome.OK,
        detail={
            "kind": body.kind.value,
            "target_host": body.target_host,
            "epoch": body.epoch,
            "reason": body.reason,
            "fencing_evidence": body.fencing_evidence,
            "expires_at": body.expires_at.isoformat(),
        },
    )
    # If auditing fails, no delivery has occurred. No writer guard is bypassed.
    await session.commit()
    acknowledgement: FailoverOverrideAcknowledgement | None = None
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                peer_url(settings, body.target_host, "/override"),
                json=body.model_dump(mode="json"),
                timeout=3.0,
            )
            response.raise_for_status()
            candidate = FailoverOverrideAcknowledgement.model_validate(response.json())
            public = (
                settings.failover_edge_a_public_key
                if body.target_host == "coire-edge-a"
                else settings.failover_edge_b_public_key
            )
            if (
                candidate.host == body.target_host
                and candidate.epoch == body.epoch
                and candidate.request_id == body.override_id
                and candidate.accepted
                and verify_ed25519(candidate.canonical_bytes(), candidate.signature, public)
            ):
                acknowledgement = candidate
    except (httpx.HTTPError, ValueError):
        pass
    await write_principal_audit(
        session,
        principal=principal,
        action=f"failover.override.{body.kind.value}",
        target_type="failover_override",
        target_id=str(body.override_id),
        outcome=AuditOutcome.OK if acknowledgement is not None else AuditOutcome.ERROR,
        detail={
            "target_host": body.target_host,
            "reason": body.reason,
            "delivered": acknowledgement is not None,
        },
    )
    await session.commit()
    if acknowledgement is None:
        raise HTTPException(503, "override delivery incomplete")
    return acknowledgement
