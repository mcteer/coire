"""Core's election member. Without a current quorum lease the frontend fences itself."""

from __future__ import annotations

from datetime import datetime

from coire_core.failover_election import ElectionParticipant, timings_from_settings
from coire_core.models.failover import FailoverMembershipConfig
from coire_core.settings import Settings


class CoreParticipant(ElectionParticipant):
    """The primary member. Readiness follows the lease, not process liveness."""

    def frontend_ready(self, now: datetime | None = None) -> bool:
        return self.serving(now)


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
    )
    # A fresh API process cannot know whether a Studio still holds the preceding lease.
    # Delay core's campaign until the hand-back window has elapsed while a Studio is visible.
    participant._outage = True
    return participant
