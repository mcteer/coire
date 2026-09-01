"""Audit records.

Append-only from the first row (spec FR-003). Feature 007 adds real actors and closes
ADR-0004; until then `actor` is the literal `admin-token` for admin actions and `anonymous`
for refusals. That is the truthful record of this period and is not rewritten later.

A refusal writes a row too: SC-002 is checked against the audit log, and "nothing happened" is
not evidence that the guard ran.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AuditOutcome(StrEnum):
    OK = "ok"
    REFUSED = "refused"
    ERROR = "error"


class AuditAction(StrEnum):
    MODEL_ADD = "model.add"
    MODEL_RETRY = "model.retry"
    MODEL_UPDATE = "model.update"
    MODEL_PUBLISH = "model.publish"
    MODEL_UNPUBLISH = "model.unpublish"
    MODEL_RETIRE = "model.retire"
    MODEL_DELETE = "model.delete"
    MODEL_READY = "model.ready"
    MODEL_FAILED = "model.failed"
    ENGINE_LOAD = "engine.load"
    ENGINE_UNLOAD = "engine.unload"
    ENGINE_RECONCILE = "engine.reconcile"
    LEDGER_UPDATE = "ledger.update"
    MODEL_PIN = "model.pin"
    MODEL_UNPIN = "model.unpin"
    PLACEMENT_REQUEST = "placement.request"
    INSTANCE_CREATE = "instance.create"
    INSTANCE_DRAIN = "instance.drain"
    INSTANCE_TRANSITION = "instance.transition"
    NODE_DECLARE = "node.declare"
    NODE_TOKEN_ROTATE = "node.registration_token.rotate"
    NODE_TOKEN_REVOKE = "node.registration_token.revoke"


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    at: datetime
    actor: str
    action: str
    target_type: str
    target_id: str
    outcome: AuditOutcome
    detail: dict[str, object] = Field(default_factory=dict)
