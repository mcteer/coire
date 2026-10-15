"""Independently fence normal gateway inference when Core failover is armed."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from coire_core.failover_authority import lease_current
from coire_core.failover_safety import ElectionSafetyError
from coire_core.failover_snapshot import SnapshotError, load_verified_snapshot
from coire_core.models.failover import FailoverLease, FailoverSnapshot
from coire_core.settings import Settings


def failover_armed(settings: Settings) -> bool:
    return bool(
        settings.failover_member_name
        or (settings.failover_core_public_key and settings.failover_peer_key.get_secret_value())
    )


def node_clock_safe(settings: Settings) -> bool:
    if not failover_armed(settings):
        return True
    # Deferred import: both routers share this check without constructing a voter.
    from coire_node.routes.failover import get_participant

    participant = get_participant()
    if participant is None or participant.safety is None:
        return False
    try:
        return participant.safety.check_clock(datetime.now(UTC))
    except ElectionSafetyError:
        return False


def holder_authority_current(
    settings: Settings,
    lease: FailoverLease,
    snapshot: FailoverSnapshot,
    *,
    existing_request: bool,
) -> bool:
    if not failover_armed(settings) or lease.holder != (
        settings.failover_member_name or settings.node_name
    ):
        return True
    from coire_node.routes.failover import get_participant

    participant = get_participant()
    if participant is None or not node_clock_safe(settings):
        return False
    if not getattr(participant._journal, "admission_available", lambda: True)():
        return False
    current = participant.lease()
    return lease_current(current, snapshot, holder=lease.holder, existing_request=existing_request)


def core_authority_guard(
    settings: Settings,
    assertion: str | None,
    *,
    guarded_upgrade_smoke: bool = False,
) -> Callable[[], bool]:
    # require_admission independently validates the 019 run-scoped smoke grant;
    # a caller-supplied flag is never sufficient to reach this exemption.
    if guarded_upgrade_smoke or not failover_armed(settings):
        return lambda: True
    if not assertion or len(assertion) > 32_768:
        return lambda: False
    try:
        lease = FailoverLease.model_validate_json(assertion)
    except ValidationError:
        return lambda: False
    path = Path(settings.failover_snapshot_path)
    fingerprint: tuple[int, int, int] | None = None
    snapshot: FailoverSnapshot | None = None
    wall = datetime.now(UTC)
    monotonic = time.monotonic()

    def allowed() -> bool:
        nonlocal fingerprint, snapshot
        now = datetime.now(UTC)
        if abs((now - wall).total_seconds() - (time.monotonic() - monotonic)) > 1:
            return False
        if not node_clock_safe(settings):
            return False
        try:
            info = path.stat()
            current = (info.st_ino, info.st_mtime_ns, info.st_size)
            if info.st_size > 16_777_216:
                return False
            if current != fingerprint:
                snapshot = load_verified_snapshot(
                    path,
                    settings.failover_core_public_key,
                    max_age_s=settings.failover_snapshot_max_age_s,
                )
                fingerprint = current
        except (OSError, SnapshotError):
            return False
        return (
            snapshot is not None
            and snapshot.membership.epoch == settings.failover_membership_epoch
            and lease_current(
                lease,
                snapshot,
                holder="coire-core",
                now=now,
            )
        )

    return allowed
