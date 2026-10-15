"""The poller classifies heartbeats and only the priority leader ends elected."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
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
from coire_core.failover_poller import FailoverPoller, HttpPeerExchange, PeerLiveness, peer_url
from coire_core.failover_safety import ElectionSafety
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
        self.heartbeats = 0
        self.vote_requests = 0

    async def heartbeat(self, peer: str, body: FailoverHeartbeat) -> tuple[bool, float]:
        del body
        self.heartbeats += 1
        if peer in self.down:
            return False, 0
        return True, 1

    async def request_vote(self, peer: str, body: ElectionVoteRequest) -> ElectionVoteGrant | None:
        self.vote_requests += 1
        if peer in self.down:
            return None
        voter = self.members[peer]
        if not verify_ed25519(
            body.canonical_bytes(), body.signature, voter.member_public_key(body.candidate)
        ):
            return None
        return voter.vote(body.epoch, body.term, body.candidate, voter._now)


async def test_current_lease_avoids_redundant_votes_but_keeps_renewal_and_fencing() -> None:
    private, membership = _material()
    timings = ElectionTimings(
        promotion=timedelta(seconds=15),
        demotion=timedelta(seconds=45),
        lease=timedelta(seconds=15),
        drain=timedelta(seconds=30),
    )
    members = {
        name: ElectionParticipant(
            name=name, membership=membership, private_key_b64=private[name], timings=timings
        )
        for name in NAMES
    }
    bus = _Bus(members)
    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    pollers = {name: FailoverPoller(member, settings, bus) for name, member in members.items()}
    now = datetime.now(UTC)
    core = members["coire-core"]

    async def tick(seconds: int) -> None:
        for name in reversed(NAMES):
            await pollers[name].tick(now + timedelta(seconds=seconds))

    await tick(0)
    assert core.serving(now)
    proof = core.proof
    requests = bus.vote_requests
    beats = bus.heartbeats
    for seconds in (2, 4, 6, 8):
        await tick(seconds)
        assert core.serving(now + timedelta(seconds=seconds))
        assert core.proof == proof
    assert bus.heartbeats == beats + 24
    assert bus.vote_requests == requests

    # Renew inside the existing lease's final third, before any authority gap.
    for seconds in range(10, 32, 2):
        await tick(seconds)
        assert core.serving(now + timedelta(seconds=seconds))
    assert bus.vote_requests > requests
    assert core.proof is not None and proof is not None
    assert min(grant.expires_at for grant in core.proof.grants) > max(
        grant.expires_at for grant in proof.grants
    )

    # Skipping redundant votes must never skip fresh quorum observations.
    bus.down.update(NAMES[1:])
    for seconds in (32, 34, 36):
        await pollers["coire-core"].tick(now + timedelta(seconds=seconds))
    assert not core.serving(now + timedelta(seconds=36))
    assert core.role is ServiceRole.STANDBY


@pytest.mark.parametrize("after_cold_guard", [False, True])
async def test_core_loss_elects_only_edge_a(after_cold_guard: bool) -> None:
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
    if after_cold_guard:
        for name in NAMES[1:]:
            members[name]._fence(outage=True)
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
    # Both Studios have rejoined and granted the healthy higher-priority Core.
    now += timedelta(seconds=2)
    for name in reversed(NAMES):
        await pollers[name].tick(now)

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


async def test_captured_signed_heartbeat_does_not_prove_fresh_liveness() -> None:
    private, membership = _material()
    timings = ElectionTimings(
        promotion=timedelta(seconds=15),
        demotion=timedelta(seconds=45),
        lease=timedelta(seconds=15),
        drain=timedelta(seconds=30),
    )
    origin = ElectionParticipant(
        name="coire-edge-a",
        membership=membership,
        private_key_b64=private["coire-edge-a"],
        timings=timings,
    )
    peer = ElectionParticipant(
        name="coire-edge-b",
        membership=membership,
        private_key_b64=private["coire-edge-b"],
        timings=timings,
    )
    now = datetime.now(UTC)
    captured = peer.issue_heartbeat(now)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=captured.model_dump(mode="json")),
        )
    ) as client:
        exchange = HttpPeerExchange(origin, Settings(_secrets_dir="/nonexistent"), client)  # type: ignore[call-arg]
        ok, _ = await exchange.heartbeat("coire-edge-b", origin.issue_heartbeat(now))
    assert not ok


def test_control_endpoints_are_declared_dns_not_bare_hostnames() -> None:
    settings = Settings(
        _secrets_dir="/nonexistent",
        failover_control_hosts={  # type: ignore[call-arg]
            "coire-core": "primary.control.lab",
            "coire-edge-a": "secondary.control.lab",
            "coire-edge-b": "tertiary.control.lab",
        },
    )
    assert (
        peer_url(settings, "coire-core", "/heartbeat")
        == "http://primary.control.lab:8180/api/v1/failover/heartbeat"
    )
    assert (
        peer_url(settings, "coire-edge-a", "/votes")
        == "http://secondary.control.lab:9400/node/failover/election/votes"
    )
    with pytest.raises(ValueError):
        Settings(
            _secrets_dir="/nonexistent",
            failover_control_hosts={  # type: ignore[call-arg]
                "coire-core": "127.0.0.1",
                "coire-edge-a": "coire-edge-a.lab",
                "coire-edge-b": "coire-edge-b.lab",
            },
        )


@pytest.mark.parametrize("restarted", NAMES)
@pytest.mark.parametrize("order", [NAMES, tuple(reversed(NAMES))])
async def test_independent_participant_restart_and_partition_never_overlap_admission(
    tmp_path: Path,
    restarted: str,
    order: tuple[str, ...],
) -> None:
    private, membership = _material()
    now = datetime.now(UTC)
    elapsed = 0.0
    timings = ElectionTimings(
        promotion=timedelta(seconds=15),
        demotion=timedelta(seconds=45),
        lease=timedelta(seconds=15),
        drain=timedelta(seconds=30),
    )
    members: dict[str, ElectionParticipant] = {}

    def member(name: str) -> ElectionParticipant:
        participant = ElectionParticipant(
            name=name, membership=membership, private_key_b64=private[name], timings=timings
        )
        participant.safety = ElectionSafety(
            tmp_path / f"{name}.json",
            member=name,
            epoch=1,
            lifetime_s=15,
            now=now,
            monotonic=lambda: elapsed,
        )
        participant.term = participant.safety.record.highest_term
        participant._issued = {
            grant.term: grant for grant in participant.safety.record.outstanding_grants
        }
        return participant

    partitions: set[tuple[str, str]] = set()

    class PartitionBus(_Bus):
        def __init__(self, sender: str) -> None:
            super().__init__(members)
            self.sender = sender

        async def heartbeat(self, peer: str, body: FailoverHeartbeat) -> tuple[bool, float]:
            if (self.sender, peer) in partitions:
                return False, 0
            return await super().heartbeat(peer, body)

        async def request_vote(
            self, peer: str, body: ElectionVoteRequest
        ) -> ElectionVoteGrant | None:
            if (self.sender, peer) in partitions:
                return None
            voter = self.members[peer]
            if not verify_ed25519(
                body.canonical_bytes(), body.signature, voter.member_public_key(body.candidate)
            ):
                return None
            return voter.vote(
                body.epoch, body.term, body.candidate, now, request_id=body.request_id
            )

    settings = Settings(_secrets_dir="/nonexistent")  # type: ignore[call-arg]
    for name in NAMES:
        members[name] = member(name)
    pollers = {name: FailoverPoller(members[name], settings, PartitionBus(name)) for name in NAMES}
    # Establish a healthy baseline *after* the mandatory first-boot quarantine.
    elapsed = 301
    now += timedelta(seconds=301)
    for _ in range(3):
        for name in order:
            await pollers[name].tick(now)
    assert members["coire-core"].serving(now)
    # Restart while its grants may still be live. This is a new object and durable reload.
    members[restarted] = member(restarted)
    pollers[restarted] = FailoverPoller(members[restarted], settings, PartitionBus(restarted))
    partitions.update((a, b) for a in NAMES for b in NAMES if a != b and "coire-core" in (a, b))
    observed_edge_a = False
    for _ in range(25):
        elapsed += 2
        now += timedelta(seconds=2)
        for name in order:
            await pollers[name].tick(now)
            # Inspect signatures/deadlines, not just the campaign roles, after each
            # independently scheduled participant. These gates feed actual origin admission.
            admitted = [
                owner
                for owner, item in members.items()
                if item.serving(now)
                and (lease := item.lease()) is not None
                and lease.proof is not None
                and lease.proof.is_valid_for(membership, now)
            ]
            assert len(admitted) <= 1
            observed_edge_a |= admitted == ["coire-edge-a"]
    assert observed_edge_a
