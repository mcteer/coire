"""The poller classifies heartbeats and only the priority leader ends elected."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from coire_core.failover_crypto import verify_ed25519
from coire_core.failover_election import (
    ElectionParticipant,
    ElectionTimings,
    MemberHealth,
    ServiceRole,
)
from coire_core.failover_poller import FailoverPoller, PeerLiveness
from coire_core.models.failover import (
    ElectionVoteGrant,
    ElectionVoteRequest,
    FailoverHeartbeat,
    FailoverMember,
    FailoverMembershipConfig,
)
from coire_core.settings import Settings

NAMES = ("coire-core", "coire-edge-a", "coire-edge-b")


def test_missed_beats_become_unreachable_and_recovery_takes_more_than_one() -> None:
    now = datetime.now(UTC)
    clock = PeerLiveness(failure_limit=3, recovery_beats=3, latency_budget_ms=50)
    clock.note("coire-core", ok=True, latency_ms=1, now=now)
    assert clock.observations("coire-edge-a", now)[1].health is MemberHealth.HEALTHY
    for _ in range(2):
        clock.note("coire-core", ok=False, latency_ms=0, now=now)
    assert all(
        item.name != "coire-core" or item.health is MemberHealth.HEALTHY
        for item in clock.observations("coire-edge-a", now)
    )
    clock.note("coire-core", ok=False, latency_ms=0, now=now)
    assert clock.observations("coire-edge-a", now)[1].health is MemberHealth.UNREACHABLE
    slow = now + timedelta(seconds=1)
    clock.note("coire-core", ok=True, latency_ms=80, now=slow)
    assert clock.observations("coire-edge-a", slow)[1].health is MemberHealth.DEGRADED
    for step in range(2):
        clock.note("coire-core", ok=True, latency_ms=1, now=slow + timedelta(seconds=step))
    assert clock.observations("coire-edge-a", slow)[1].health is MemberHealth.DEGRADED
    clock.note("coire-core", ok=True, latency_ms=1, now=slow + timedelta(seconds=3))
    assert clock.observations("coire-edge-a", slow)[1].health is MemberHealth.HEALTHY


def _material() -> tuple[dict[str, str], FailoverMembershipConfig]:
    private: dict[str, str] = {}
    members: list[FailoverMember] = []
    for priority, name in enumerate(NAMES):
        key = Ed25519PrivateKey.generate()
        private[name] = base64.b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode()
        public = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
        members.append(FailoverMember(name=name, priority=priority, public_key=public))
    membership = FailoverMembershipConfig(epoch=1, signing_key_id="core-1", members=members)
    return private, membership


def _role(participant: ElectionParticipant) -> ServiceRole:
    return participant.role


class _Bus:
    def __init__(self, members: dict[str, ElectionParticipant]) -> None:
        self.members = members
        self.down: set[str] = set()

    async def heartbeat(self, peer: str, body: FailoverHeartbeat) -> tuple[bool, float]:
        del body
        if peer in self.down:
            return False, 0
        return True, 1

    async def request_vote(self, peer: str, body: ElectionVoteRequest) -> ElectionVoteGrant | None:
        if peer in self.down:
            return None
        voter = self.members[peer]
        if not verify_ed25519(
            body.canonical_bytes(), body.signature, voter.member_public_key(body.candidate)
        ):
            return None
        return voter.vote(body.epoch, body.term, body.candidate, voter._now)


async def test_core_loss_elects_only_edge_a() -> None:
    private, membership = _material()
    timings = ElectionTimings(
        promotion=timedelta(seconds=15),
        demotion=timedelta(seconds=45),
        lease=timedelta(seconds=15),
        drain=timedelta(seconds=30),
    )
    members = {
        name: ElectionParticipant(
            name=name,
            membership=membership,
            private_key_b64=private[name],
            timings=timings,
        )
        for name in NAMES
    }
    bus = _Bus(members)
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_peer_key=SecretStr("unused"),
        node_probe_failures_before_unreachable=3,
    )
    pollers = {name: FailoverPoller(member, settings, bus) for name, member in members.items()}
    now = datetime.now(UTC)
    for name in ("coire-edge-b", "coire-edge-a", "coire-core"):
        await pollers[name].tick(now)
    assert _role(members["coire-core"]) is ServiceRole.ELECTED
    assert _role(members["coire-edge-a"]) is not ServiceRole.ELECTED

    bus.down.add("coire-core")
    for step in range(3):
        moment = now + timedelta(seconds=2 * step)
        await pollers["coire-edge-b"].tick(moment)
        await pollers["coire-edge-a"].tick(moment)
    elected = now + timedelta(seconds=15)
    for _ in range(3):
        await pollers["coire-edge-b"].tick(elected)
        await pollers["coire-edge-a"].tick(elected)
    assert _role(members["coire-edge-a"]) is ServiceRole.ELECTED
    assert _role(members["coire-edge-b"]) is not ServiceRole.ELECTED
