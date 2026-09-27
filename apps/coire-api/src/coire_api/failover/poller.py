"""Start core's election poller when this process is the configured member."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from coire_api.failover.participant import CoreParticipant, core_participant
from coire_api.failover.publication import configured_membership
from coire_api.routes.failover import set_participant
from coire_core.failover_crypto import sign_ed25519
from coire_core.failover_election import ServiceRole
from coire_core.failover_poller import FailoverPoller, peer_url
from coire_core.models.failover import HandbackNotice
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class CoreFailoverPoller(FailoverPoller):
    """Send signed drain requests once core has passed its recovery damping."""

    def __init__(self, participant: CoreParticipant, settings: Settings) -> None:
        super().__init__(participant, settings)
        self._handback_client = httpx.AsyncClient()
        self._signing_key = settings.failover_peer_key.get_secret_value()
        self._settings = settings
        self._ack_term = participant.term
        self._acked: set[str] = set()

    async def tick(self, now: datetime | None = None) -> None:
        await super().tick(now)
        if self.participant.role not in (ServiceRole.CANDIDATE, ServiceRole.ELECTED):
            return
        if self.participant.term != self._ack_term:
            self._ack_term = self.participant.term
            self._acked.clear()
        moment = now or datetime.now(UTC)
        for holder in ("coire-edge-a", "coire-edge-b"):
            if holder in self._acked:
                continue
            unsigned = HandbackNotice(
                epoch=self.participant.membership.epoch,
                term=self.participant.term,
                holder=holder,
                successor="coire-core",
                expires_at=moment + timedelta(seconds=30),
                signature="unsigned",
            )
            notice = unsigned.model_copy(
                update={"signature": sign_ed25519(unsigned.canonical_bytes(), self._signing_key)}
            )
            try:
                response = await self._handback_client.post(
                    peer_url(self._settings, holder, "/handback"),
                    json=notice.model_dump(mode="json"),
                    timeout=1.0,
                )
                if response.status_code != 204:
                    logger.warning("failover hand-back not acknowledged by %s", holder)
                else:
                    self._acked.add(holder)
            except httpx.HTTPError:
                logger.warning("failover hand-back unreachable at %s", holder)

    async def stop(self) -> None:
        await super().stop()
        await self._handback_client.aclose()


def build_poller(settings: Settings) -> FailoverPoller | None:
    """Install the core member and return its poller, or nothing when failover is unarmed."""
    name = settings.failover_member_name.strip()
    if name != "coire-core" or not settings.failover_peer_key.get_secret_value().strip():
        return None
    membership = configured_membership(settings)
    if membership is None:
        logger.warning("failover poller not started; membership is incomplete")
        return None
    participant = core_participant(settings, membership)
    if participant is None:
        return None
    set_participant(participant)
    return CoreFailoverPoller(participant, settings)
