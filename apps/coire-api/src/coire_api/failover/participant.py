"""Core's election member. Without a current quorum lease the frontend fences itself."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

from coire_core.failover_election import ElectionParticipant, timings_from_settings
from coire_core.models.failover import FailoverHeartbeat, FailoverMembershipConfig
from coire_core.settings import Settings


class CoreParticipant(ElectionParticipant):
    """The primary member. Readiness follows the lease, not process liveness."""

    recovery_deadline: float | None = None

    def cohort_current(self) -> bool:
        return self.recovery_ready and (
            self.recovery_deadline is None or time.monotonic() < self.recovery_deadline
        )

    def frontend_ready(self, now: datetime | None = None) -> bool:
        return self.cohort_current() and self.serving(now)

    def issue_heartbeat(self, now: datetime, challenge: UUID | None = None) -> FailoverHeartbeat:
        if not self.cohort_current():
            self.recovery_ready = False
        return super().issue_heartbeat(now, challenge)


def core_participant(
    settings: Settings, membership: FailoverMembershipConfig
) -> CoreParticipant | None:
    """Build the core member when its Keychain signing key is present."""
    private_key = settings.failover_peer_key.get_secret_value().strip()
    if not private_key:
        return None
    participant = CoreParticipant(
        name="coire-core",
        membership=membership,
        private_key_b64=private_key,
        timings=timings_from_settings(settings),
        safety_path=(
            Path(settings.failover_safety_path)
            if settings.failover_safety_path
            else Path(settings.failover_snapshot_path).with_name("coire-core.safety.json")
        ),
    )
    # A fresh API process cannot know whether a Studio still holds the preceding lease.
    # Delay core's campaign until the hand-back window has elapsed while a Studio is visible.
    participant.recovery_ready = False
    participant._outage = True
    return participant
