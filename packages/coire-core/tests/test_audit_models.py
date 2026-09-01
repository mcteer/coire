from __future__ import annotations

import uuid
from datetime import UTC, datetime

from coire_core.models.audit import AuditOutcome, AuditRecord
from coire_core.models.auth import ActorType


def test_extended_audit_shape_is_strict_and_legacy_compatible() -> None:
    record = AuditRecord(
        id=uuid.uuid4(),
        at=datetime.now(UTC),
        actor="coire-scheduler",
        action="instance.transition",
        target_type="instance",
        target_id="01",
        outcome=AuditOutcome.OK,
    )
    assert record.actor_type is ActorType.SERVICE
    assert record.before == {}
    assert record.after == {}
    assert record.context == {}
