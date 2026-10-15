"""Start core's election poller when this process is the configured member."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx

from coire_api.db import session_scope
from coire_api.failover.cohort import authoritative_cohort_ready
from coire_api.failover.participant import CoreParticipant, core_participant
from coire_api.failover.publication import configured_membership
from coire_api.failover.reconcile import reconcile_events
from coire_api.routes.failover import set_participant
from coire_api.upgrades.admission import CoreAdmissions
from coire_core.failover_crypto import sign_ed25519, verify_ed25519
from coire_core.failover_election import ServiceRole
from coire_core.failover_journal import ElectionJournal
from coire_core.failover_poller import FailoverPoller, peer_url
from coire_core.models.failover import HandbackAcknowledgement, HandbackNotice
from coire_core.settings import Settings

logger = logging.getLogger(__name__)


class CoreFailoverPoller(FailoverPoller):
    """Send signed drain requests once core has passed its recovery damping."""

    def __init__(
        self,
        participant: CoreParticipant,
        settings: Settings,
        admissions: CoreAdmissions | None = None,
    ) -> None:
        super().__init__(participant, settings)
        self._admissions = admissions or CoreAdmissions(enabled=settings.upgrades_enabled)
        self._core = participant
        self._handback_client = httpx.AsyncClient()
        self._signing_key = settings.failover_peer_key.get_secret_value()
        self._settings = settings
        self._ack_term = participant.term
        self._acked: set[str] = set()

    async def tick(self, now: datetime | None = None) -> None:
        await self._flush_events()
        previously_ready = self._core.cohort_current()
        self.participant.recovery_ready = await authoritative_cohort_ready(
            self._settings, self._handback_client
        )
        if previously_ready != self.participant.recovery_ready:
            logger.info(
                "failover Core cohort readiness changed",
                extra={"host": "coire-core", "recovery_ready": self.participant.recovery_ready},
            )
        self._core.recovery_deadline = (
            time.monotonic() + 2 * self._settings.failover_election_interval_s + 1
        )
        await super().tick(now)
        if not self._core.cohort_current():
            return
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
                issued_at=moment,
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
                response.raise_for_status()
                acknowledgement = HandbackAcknowledgement.model_validate(response.json())
                public = next(
                    member.public_key
                    for member in self.participant.membership.members
                    if member.name == holder
                )
                checked_at = now or datetime.now(UTC)
                if (
                    acknowledgement.request_id == notice.request_id
                    and acknowledgement.host == holder
                    and acknowledgement.epoch == notice.epoch
                    and acknowledgement.term == notice.term
                    and acknowledgement.successor == notice.successor
                    and acknowledgement.accepted
                    and acknowledgement.fenced
                    and checked_at < acknowledgement.expires_at <= notice.expires_at
                    and verify_ed25519(
                        acknowledgement.canonical_bytes(), acknowledgement.signature, public
                    )
                ):
                    self._acked.add(holder)
                else:
                    logger.warning("failover hand-back not fenced or correlated by %s", holder)
            except (httpx.HTTPError, ValueError):
                logger.warning("failover hand-back unreachable at %s", holder)

    async def _flush_events(self) -> None:
        journal = self.participant._journal
        if not isinstance(journal, ElectionJournal):
            return
        try:
            async with asyncio.timeout(1):
                entries = await asyncio.to_thread(journal.entries)
                if not entries:
                    return
                async with self._admissions.admit(("coire-api",)), session_scope() as session:
                    await reconcile_events(session, entries)
                # session_scope commits before acknowledgement. A commit failure or active
                # 019 fence preserves every event; duplicate retries use durable receipt IDs.
                await asyncio.to_thread(journal.acknowledge, [item.event_id for item in entries])
        except Exception:
            logger.warning(
                "Core failover audit reconciliation deferred", extra={"host": "coire-core"}
            )

    async def stop(self) -> None:
        await super().stop()
        await self._handback_client.aclose()


def build_poller(
    settings: Settings, admissions: CoreAdmissions | None = None
) -> FailoverPoller | None:
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
    return CoreFailoverPoller(participant, settings, admissions)
