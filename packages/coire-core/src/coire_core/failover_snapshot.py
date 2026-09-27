"""Read a signed failover snapshot. Publication stays on core."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from coire_core.models.failover import FailoverSnapshot


class SnapshotError(ValueError):
    """A snapshot is unsafe to use for degraded service."""


def load_verified_snapshot(
    path: Path,
    trusted_core_public_key: str,
    *,
    max_age_s: float = 120.0,
    now: datetime | None = None,
) -> FailoverSnapshot:
    """Load a snapshot only if it is signed and fresh; every error fences service."""
    try:
        snapshot = FailoverSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotError("snapshot is missing or malformed") from exc
    if not snapshot.signature_is_valid(trusted_core_public_key):
        raise SnapshotError("snapshot signature is invalid")
    if not snapshot.is_fresh(max_age_s, now or datetime.now(UTC)):
        raise SnapshotError("snapshot is stale or expired")
    return snapshot
