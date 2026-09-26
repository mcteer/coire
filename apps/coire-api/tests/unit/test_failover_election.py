"""Election rules for core: replay, competing terms, priority delay, quorum, and hand-back."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coire_api.failover.participant import CoreParticipant
from coire_core.failover_crypto import sign_ed25519
from coire_core.failover_election import (
    ElectionParticipant,
    ElectionTimings,
    MemberHealth,
    MemberObservation,
    ServiceRole,
)
from coire_core.models.failover import (
    ElectionVoteGrant,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverOverride,
    FailoverOverrideKind,
)

NAMES = ("coire-core", "coire-edge-a", "coire-edge-b")
PRIORITIES = {"coire-core": 0, "coire-edge-a": 1, "coire-edge-b": 2}


def _keys() -> dict[str, tuple[str, str]]:
    material: dict[str, tuple[str, str]] = {}
    for name in NAMES:
        private = Ed25519PrivateKey.generate()
        private_b64 = base64.b64encode(
            private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode()
        public_b64 = base64.b64encode(
            private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
        material[name] = (private_b64, public_b64)
    return material


def _membership(material: dict[str, tuple[str, str]]) -> FailoverMembershipConfig:
    return FailoverMembershipConfig(
        epoch=1,
        signing_key_id="core-1",
        members=[
            FailoverMember(name=name, priority=PRIORITIES[name], public_key=material[name][1])
            for name in NAMES
        ],
    )


def _timings() -> ElectionTimings:
    return ElectionTimings(
        promotion=timedelta(seconds=10),
        demotion=timedelta(seconds=30),
        lease=timedelta(minutes=5),
        drain=timedelta(seconds=30),
    )


def _participants(
    material: dict[str, tuple[str, str]], membership: FailoverMembershipConfig
) -> dict[str, ElectionParticipant]:
    return {
        name: (
            CoreParticipant(
                name=name,
                membership=membership,
                private_key_b64=material[name][0],
                timings=_timings(),
            )
            if name == "coire-core"
            else ElectionParticipant(
                name=name,
                membership=membership,
                private_key_b64=material[name][0],
                timings=_timings(),
                standing_reservation=True,
            )
        )
        for name in NAMES
    }


def _view(now: datetime, **health: str) -> list[MemberObservation]:
    since = now - timedelta(seconds=health.pop("age", 20))  # type: ignore[arg-type]
    return [
        MemberObservation(name=name, health=MemberHealth(state), since=since)
        for name, state in health.items()
    ]


def _settle(
    members: dict[str, ElectionParticipant],
    now: datetime,
    *,
    absent: frozenset[str] = frozenset(),
    health: dict[str, str] | None = None,
    recovery_age_s: int = 20,
) -> None:
    assumed = health or dict.fromkeys(NAMES, "healthy")
    observations = {
        name: _view(
            now,
            **{
                peer: ("unreachable" if peer in absent or peer == name else assumed[peer])
                for peer in NAMES
            },
        )
        for name in NAMES
    }
    # A member always sees itself. Peers listed in `absent` are unreachable to everyone.
    for name in NAMES:
        observations[name] = [
            MemberObservation(
                name=peer,
                health=MemberHealth.HEALTHY
                if peer == name
                else MemberHealth.UNREACHABLE
                if peer in absent
                else MemberHealth(assumed[peer]),
                since=now - timedelta(seconds=recovery_age_s),
            )
            for peer in NAMES
        ]
    for _ in range(3):
        for name, participant in members.items():
            if name in absent:
                continue
            participant.observe(observations[name], now)
        for candidate in members.values():
            if candidate.name in absent or candidate.role not in (
                ServiceRole.CANDIDATE,
                ServiceRole.ELECTED,
            ):
                continue
            for voter in members.values():
                if voter.name in absent or voter.name == candidate.name:
                    continue
                grant = voter.vote(1, candidate.term, candidate.name, now)
                if grant is not None:
                    candidate.receive_grant(grant, now)


def test_elected_lease_renews_before_readiness_expires() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    now = datetime.now(UTC)
    _settle(members, now, absent=frozenset({"coire-core"}))
    edge_a = members["coire-edge-a"]
    edge_b = members["coire-edge-b"]
    assert edge_a.serving(now)
    near_expiry = now + _timings().lease * 3 / 4
    view = [
        MemberObservation(
            name=name,
            health=MemberHealth.UNREACHABLE if name == "coire-core" else MemberHealth.HEALTHY,
            since=near_expiry - timedelta(seconds=20),
        )
        for name in NAMES
    ]
    edge_a.observe(view, near_expiry)
    renewal = edge_b.vote(1, edge_a.term, "coire-edge-a", near_expiry)
    assert renewal is not None and edge_a.receive_grant(renewal, near_expiry)
    edge_a.observe(view, near_expiry)
    assert edge_a.serving(now + _timings().lease + timedelta(seconds=1))


def test_term_change_does_not_forget_an_unexpired_competing_vote() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    edge_b = members["coire-edge-b"]
    now = datetime.now(UTC)
    edge_b.observe(
        [
            MemberObservation(
                name=name,
                health=MemberHealth.UNREACHABLE if name == "coire-core" else MemberHealth.HEALTHY,
                since=now - timedelta(seconds=20),
            )
            for name in NAMES
        ],
        now,
    )
    prior = edge_b.vote(1, 1, "coire-edge-a", now)
    assert prior is not None
    edge_b.adopt_term(2)
    edge_b.observe(
        [MemberObservation(name=name, health=MemberHealth.HEALTHY, since=now) for name in NAMES],
        now,
    )
    assert edge_b.vote(1, 2, "coire-core", now) is None
    assert edge_b.vote(1, 1, "coire-core", prior.expires_at + timedelta(seconds=1)) is None
    assert edge_b.vote(1, 2, "coire-core", prior.expires_at + timedelta(seconds=1)) is not None


def _override(private_key: str, kind: FailoverOverrideKind, expires: datetime) -> FailoverOverride:
    unsigned = FailoverOverride(
        kind=kind,
        actor_id=uuid4(),
        reason="operator action",
        expires_at=expires,
        signature="unsigned",
    )
    return unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), private_key)}
    )


def test_vote_replay_returns_the_same_grant_and_refuses_a_second_candidate() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    now = datetime.now(UTC)
    _settle(members, now)
    edge_b = members["coire-edge-b"]
    first = edge_b.vote(1, members["coire-core"].term, "coire-core", now)
    second = edge_b.vote(1, members["coire-core"].term, "coire-core", now)
    assert first is not None and second is not None
    assert first.signature == second.signature
    assert edge_b.vote(1, members["coire-core"].term, "coire-edge-a", now) is None


def test_an_older_term_cannot_displace_the_current_lease() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    now = datetime.now(UTC)
    _settle(members, now, absent=frozenset({"coire-core"}))
    _settle(members, now)
    core = members["coire-core"]
    assert isinstance(core, CoreParticipant)
    assert not core.frontend_ready(now)
    reclaimed = now + _timings().lease + timedelta(seconds=1)
    _settle(members, reclaimed, recovery_age_s=31)
    assert core.frontend_ready(reclaimed)
    stale = ElectionVoteGrant(
        epoch=1,
        term=1,
        candidate="coire-core",
        voter="coire-edge-a",
        expires_at=now + timedelta(seconds=30),
        signature="unsigned",
    )
    stale = stale.model_copy(
        update={"signature": sign_ed25519(stale.canonical_bytes(), material["coire-edge-a"][0])}
    )
    if core.term == 1:
        core.observe(
            [
                MemberObservation(
                    name=peer,
                    health=MemberHealth.UNREACHABLE,
                    since=now - timedelta(seconds=30),
                )
                for peer in NAMES
            ],
            now,
        )
        _settle(members, now)
        now = now + timedelta(seconds=30)
        _settle(members, now)
    assert core.term > 1
    assert not core.receive_grant(stale, now)
    assert core.frontend_ready(now)


def test_priority_delay_waits_until_core_is_unreachable_past_the_threshold() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    now = datetime.now(UTC)
    waiting = [
        MemberObservation(name="coire-core", health=MemberHealth.UNREACHABLE, since=now),
        MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now),
        MemberObservation(name="coire-edge-b", health=MemberHealth.HEALTHY, since=now),
    ]
    assert members["coire-edge-a"].observe(waiting, now) is ServiceRole.STANDBY
    confirmed = [
        MemberObservation(
            name="coire-core",
            health=MemberHealth.UNREACHABLE,
            since=now - timedelta(seconds=10),
        ),
        MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now),
        MemberObservation(name="coire-edge-b", health=MemberHealth.HEALTHY, since=now),
    ]
    assert members["coire-edge-a"].observe(confirmed, now) is ServiceRole.CANDIDATE


def test_degraded_core_is_not_treated_as_down() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    now = datetime.now(UTC)
    degraded = [
        MemberObservation(
            name="coire-core", health=MemberHealth.DEGRADED, since=now - timedelta(minutes=5)
        ),
        MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now),
        MemberObservation(name="coire-edge-b", health=MemberHealth.HEALTHY, since=now),
    ]
    assert members["coire-edge-a"].observe(degraded, now) is ServiceRole.STANDBY
    assert members["coire-edge-a"].eligible_leader(now) == "coire-core"


def test_quorum_loss_fences_core_and_a_promoted_studio() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    now = datetime.now(UTC)
    _settle(members, now, absent=frozenset({"coire-core"}))
    edge_a = members["coire-edge-a"]
    assert edge_a.serving(now)
    alone = [
        MemberObservation(
            name=peer, health=MemberHealth.UNREACHABLE, since=now - timedelta(seconds=30)
        )
        for peer in NAMES
        if peer != "coire-edge-a"
    ]
    alone.append(MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now))
    assert edge_a.observe(alone, now) is ServiceRole.STANDBY
    assert not edge_a.serving(now)
    core = members["coire-core"]
    assert isinstance(core, CoreParticipant)
    core.observe(alone, now)
    assert not core.frontend_ready(now)


def test_hand_back_drains_in_flight_work_and_keeps_the_reservation() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    now = datetime.now(UTC)
    _settle(members, now, absent=frozenset({"coire-core"}))
    edge_a = members["coire-edge-a"]
    assert edge_a.reservation_held
    assert edge_a.engines_evicted == 0
    recovered = [
        MemberObservation(
            name="coire-core", health=MemberHealth.HEALTHY, since=now - timedelta(seconds=5)
        ),
        MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now),
        MemberObservation(name="coire-edge-b", health=MemberHealth.HEALTHY, since=now),
    ]
    assert edge_a.observe(recovered, now, in_flight=1) is ServiceRole.ELECTED
    ready = [
        MemberObservation(
            name="coire-core", health=MemberHealth.HEALTHY, since=now - timedelta(seconds=30)
        ),
        MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now),
        MemberObservation(name="coire-edge-b", health=MemberHealth.HEALTHY, since=now),
    ]
    assert edge_a.observe(ready, now, in_flight=1) is ServiceRole.DRAINING
    assert not edge_a.serving(now)
    assert edge_a.reservation_held
    assert edge_a.observe(ready, now, in_flight=0) is ServiceRole.STANDBY
    assert edge_a.reservation_held


def test_inhibit_blocks_studio_promotion() -> None:
    material = _keys()
    members = _participants(material, _membership(material))
    now = datetime.now(UTC)
    override = _override(
        material["coire-core"][0],
        FailoverOverrideKind.INHIBIT,
        now + timedelta(minutes=5),
    )
    confirmed = [
        MemberObservation(
            name="coire-core",
            health=MemberHealth.UNREACHABLE,
            since=now - timedelta(seconds=30),
        ),
        MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now),
        MemberObservation(name="coire-edge-b", health=MemberHealth.HEALTHY, since=now),
    ]
    assert members["coire-edge-a"].observe(confirmed, now, override=override) is ServiceRole.STANDBY
