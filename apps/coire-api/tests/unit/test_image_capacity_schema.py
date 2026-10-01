"""Capacity schema invariants required before job admission exists."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, Index

from coire_api.db import Base


def test_quota_rows_have_unique_owner_and_global_scopes() -> None:
    table = Base.metadata.tables["image_quotas"]
    indexes = {str(index.name): index for index in table.indexes}
    assert indexes["uq_image_quota_owner"].unique
    assert indexes["uq_image_quota_global"].unique
    assert all(isinstance(index, Index) for index in indexes.values())
    assert any(
        isinstance(c, CheckConstraint)
        and all(
            name in str(c.sqltext)
            for name in (
                "held_bytes",
                "stored_bytes",
                "pending_jobs",
                "held_outputs",
                "consumed_outputs",
            )
        )
        for c in table.constraints
    )


def test_lease_has_single_subject_and_release_evidence() -> None:
    table = Base.metadata.tables["image_execution_leases"]
    assert {
        "node_id",
        "job_id",
        "request_id",
        "fence",
        "heartbeat_at",
        "expires_at",
        "released_at",
        "release_evidence",
    } <= set(table.c.keys())
    assert any(
        isinstance(c, CheckConstraint)
        and "job_id IS NOT NULL" in str(c.sqltext)
        and "request_id IS NOT NULL" in str(c.sqltext)
        for c in table.constraints
    )
    profiles = Base.metadata.tables["image_coexistence_profiles"]
    assert {
        "node_id",
        "hardware_fingerprint",
        "runtime_fingerprint",
        "chat_variant_ids",
        "image_model_id",
        "first_token_p95_ms",
        "status",
    } <= set(profiles.c.keys())


def test_capacity_migration_has_guarded_reverse() -> None:
    path = Path("apps/coire-api/alembic/versions/0025_image_capacity.py")
    migration = runpy.run_path(str(path))
    assert migration["down_revision"] == "0024_image_assets"

    class OccupiedBind:
        def scalar(self, statement: object) -> int:
            return 1

    class OccupiedOp:
        def get_bind(self) -> OccupiedBind:
            return OccupiedBind()

    migration["downgrade"].__globals__["op"] = OccupiedOp()
    with pytest.raises(RuntimeError, match="image capacity records"):
        migration["downgrade"]()
