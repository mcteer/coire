"""Private image schema safety contracts."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, UniqueConstraint

from coire_api.db import Base


def test_image_job_identity_and_publication_evidence_are_durable() -> None:
    table = Base.metadata.tables["image_jobs"]
    assert {
        "owner_user_id",
        "idempotency_key",
        "intent_sha256",
        "submitted_spec",
        "resolved_spec",
        "state",
        "version",
        "attempt",
        "fence",
        "cancel_requested_at",
        "receipt_state",
        "cleanup_state",
        "authorization_snapshot",
    } <= set(table.c.keys())
    assert any(
        isinstance(c, UniqueConstraint)
        and {"owner_user_id", "idempotency_key"} == {x.name for x in c.columns}
        for c in table.constraints
    )
    assert any(
        isinstance(c, CheckConstraint) and "^[0-9A-HJKMNP-TV-Z]{26}$" in str(c.sqltext)
        for c in table.constraints
    )
    assert any(
        isinstance(fk, ForeignKey) and fk.target_fullname == "users.id"
        for fk in table.c.owner_user_id.foreign_keys
    )
    assert any(
        isinstance(c, ForeignKeyConstraint)
        and {fk.target_fullname for fk in c.elements}
        == {"image_preset_revisions.preset_id", "image_preset_revisions.revision"}
        for c in table.constraints
    )


def test_image_event_and_preset_revision_keys() -> None:
    events = Base.metadata.tables["image_job_events"]
    assert {"job_id", "sequence"} == {c.name for c in events.primary_key.columns}
    revisions = Base.metadata.tables["image_preset_revisions"]
    assert {"preset_id", "revision"} == {c.name for c in revisions.primary_key.columns}
    assert {
        "defaults",
        "prefix",
        "dependency_ids",
        "entitlement_requirements",
        "created_by_user_id",
    } <= set(revisions.c.keys())


def test_image_schema_migration_has_guarded_reverse() -> None:
    path = Path("apps/coire-api/alembic/versions/0023_image_jobs_presets.py")
    namespace = runpy.run_path(str(path))
    assert namespace["down_revision"] == "0022_stopped_usage_outcome"
    assert "image_preset_revision_immutable" in path.read_text()

    class OccupiedBind:
        def scalar(self, statement: object) -> int:
            return 1

    class OccupiedOp:
        def get_bind(self) -> OccupiedBind:
            return OccupiedBind()

    namespace["downgrade"].__globals__["op"] = OccupiedOp()
    with pytest.raises(RuntimeError, match="image records"):
        namespace["downgrade"]()
