"""The audit trail.

Every administrative mutation writes a row, and so does every refusal (spec FR-003, SC-002):
"nothing happened" is not evidence that the guard ran, and a security control with no record
of having fired cannot be tested.

Append-only. Nothing in this module updates or deletes, and no route exposes a way to.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coire_api.db import AuditRow
from coire_core.models.audit import AuditOutcome

logger = logging.getLogger(__name__)

_SECRET_HINTS = ("token", "secret", "password", "credential", "authorization", "bearer", "grant")
_MAX_VALUE_CHARS = 500


def redact(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Drop anything that looks like a credential, recursively.

    Audit detail is assembled from request bodies and node responses, both of which can carry
    a token by accident. Redacting on the way in is the only placement that cannot be
    forgotten at a call site — and an audit row is exactly the sort of long-lived record you
    do not want a secret to land in.
    """
    if not detail:
        return {}
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            out[key] = "[redacted]"
        elif isinstance(value, dict):
            out[key] = redact(value)
        elif isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
            out[key] = value[:_MAX_VALUE_CHARS] + "…"
        else:
            out[key] = value
    return out


async def write_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    outcome: AuditOutcome = AuditOutcome.OK,
    detail: dict[str, Any] | None = None,
) -> AuditRow:
    """Append one audit record.

    Flushes but does not commit: an audit row belongs to the same transaction as the mutation
    it records, so a rolled-back mutation does not leave a row claiming it happened. Refusals
    are the exception and commit their own session — see `auth.require_admin`.
    """
    row = AuditRow(
        actor=actor,
        action=str(action),
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        detail=redact(detail),
    )
    session.add(row)
    await session.flush()
    logger.info(
        "audit actor=%s action=%s target=%s/%s outcome=%s",
        actor,
        action,
        target_type,
        target_id,
        outcome.value,
    )
    return row
