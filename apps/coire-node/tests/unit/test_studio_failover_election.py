"""Studio election: proof fencing, lone survivor, break-glass, and hand-back."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coire_core.failover_crypto import sign_ed25519
from coire_core.failover_election import (
    ElectionTimings,
    MemberHealth,
    MemberObservation,
    ServiceRole,
)
from coire_core.models.failover import (
    FailoverLease,
    FailoverMember,
    FailoverMembershipConfig,
    FailoverOverride,
    FailoverOverrideKind,
)
from coire_node.failover.journal import ElectionJournal
from coire_node.failover.participant import StudioParticipant

NAMES = ("coire-core", "coire-edge-a", "coire-edge-b")
PRIORITIES = {"coire-core": 0, "coire-edge-a": 1, "coire-edge-b": 2}


def _material() -> tuple[dict[str, str], dict[str, str], FailoverMembershipConfig]:
    private: dict[str, str] = {}
    public: dict[str, str] = {}
    for name in NAMES:
        key = Ed25519PrivateKey.generate()
        private[name] = base64.b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode()
        public[name] = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
    membership = FailoverMembershipConfig(
        epoch=1,
        signing_key_id="core-1",
        members=[
            FailoverMember(name=name, priority=PRIORITIES[name], public_key=public[name])
            for name in NAMES
        ],
    )
    return private, public, membership


def _studio(
    name: str, private: dict[str, str], membership: FailoverMembershipConfig, path: Path
) -> StudioParticipant:
    return StudioParticipant(
        name=name,
        membership=membership,
        private_key_b64=private[name],
        timings=ElectionTimings(
            promotion=timedelta(seconds=10),
            demotion=timedelta(seconds=30),
            lease=timedelta(minutes=5),
            drain=timedelta(seconds=30),
        ),
        proof_path=path,
        journal=ElectionJournal(path.parent / f"{name}.json"),
    )


def _seen(now: datetime, self_name: str, down: frozenset[str]) -> list[MemberObservation]:
    return [
        MemberObservation(
            name=peer,
            health=MemberHealth.HEALTHY if peer not in down else MemberHealth.UNREACHABLE,
            since=now - timedelta(seconds=20),
        )
        for peer in NAMES
    ]


def test_vote_replay_and_competing_term_and_priority_delay(tmp_path: Path) -> None:
    private, _, membership = _material()
    edge_a = _studio("coire-edge-a", private, membership, tmp_path / "a.json")
    edge_b = _studio("coire-edge-b", private, membership, tmp_path / "b.json")
    now = datetime.now(UTC)
    waiting = [
        MemberObservation(name="coire-core", health=MemberHealth.UNREACHABLE, since=now),
        MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now),
        MemberObservation(name="coire-edge-b", health=MemberHealth.HEALTHY, since=now),
    ]
    assert edge_a.observe(waiting, now) is ServiceRole.STANDBY
    confirmed = _seen(now, "coire-edge-a", frozenset({"coire-core"}))
    assert edge_a.observe(confirmed, now) is ServiceRole.CANDIDATE
    edge_b.observe(confirmed, now)
    first = edge_b.vote(1, edge_a.term, "coire-edge-a", now)
    assert first is not None and edge_b.vote(1, edge_a.term, "coire-edge-a", now) == first
    assert edge_b.vote(1, edge_a.term, "coire-edge-b", now) is None
    assert edge_a.receive_grant(first, now)
    assert edge_a.observe(confirmed, now) is ServiceRole.ELECTED
    stale = first.model_copy(update={"term": 1})
    edge_a.term = edge_a.term + 1
    assert not edge_a.receive_grant(stale, now)


def test_restart_fences_old_lease_and_waits_before_voting(tmp_path: Path) -> None:
    private, _, membership = _material()
    path = tmp_path / "lease.json"
    path.write_text("old signed proof")
    timings = ElectionTimings(
        promotion=timedelta(seconds=10),
        demotion=timedelta(seconds=30),
        lease=timedelta(seconds=15),
        drain=timedelta(seconds=30),
    )
    edge_b = StudioParticipant(
        name="coire-edge-b",
        membership=membership,
        private_key_b64=private["coire-edge-b"],
        timings=timings,
        proof_path=path,
        restart_hold=True,
    )
    now = datetime.now(UTC)
    assert not path.exists()
    view = _seen(now, "coire-edge-b", frozenset({"coire-core"}))
    edge_b.observe(view, now)
    assert edge_b.vote(1, 1, "coire-edge-a", now) is None
    later = now + timedelta(seconds=16)
    edge_b.observe(_seen(later, "coire-edge-b", frozenset({"coire-core"})), later)
    assert edge_b.vote(1, 1, "coire-edge-a", later) is not None


def test_quorum_loss_deletes_the_lease_file(tmp_path: Path) -> None:
    private, _, membership = _material()
    edge_a = _studio("coire-edge-a", private, membership, tmp_path / "lease.json")
    edge_b = _studio("coire-edge-b", private, membership, tmp_path / "b.json")
    now = datetime.now(UTC)
    view = _seen(now, "coire-edge-a", frozenset({"coire-core"}))
    edge_a.observe(view, now)
    edge_b.observe(view, now)
    grant = edge_b.vote(1, edge_a.term, "coire-edge-a", now)
    assert grant is not None
    edge_a.receive_grant(grant, now)
    assert edge_a.observe(view, now) is ServiceRole.ELECTED
    assert FailoverLease.model_validate_json(edge_a.proof_path.read_text()).holder == "coire-edge-a"
    assert edge_a.engines_evicted == 0
    alone = _seen(now, "coire-edge-a", frozenset({"coire-core", "coire-edge-b"}))
    assert edge_a.observe(alone, now) is ServiceRole.STANDBY
    assert not edge_a.proof_path.exists()
    assert not edge_a.serving(now)


def test_lone_survivor_fails_closed_until_break_glass(tmp_path: Path) -> None:
    private, _, membership = _material()
    edge_b = _studio("coire-edge-b", private, membership, tmp_path / "lease.json")
    now = datetime.now(UTC)
    alone = _seen(now, "coire-edge-b", frozenset({"coire-core", "coire-edge-a"}))
    assert edge_b.observe(alone, now) is ServiceRole.STANDBY
    assert not edge_b.proof_path.exists()
    unsigned = FailoverOverride(
        kind=FailoverOverrideKind.BREAK_GLASS_PROMOTE,
        actor_id=uuid4(),
        reason="both peers are down",
        expires_at=now + timedelta(minutes=5),
        signature="unsigned",
    )
    override = unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), private["coire-core"])}
    )
    assert edge_b.observe(alone, now, override=override) is ServiceRole.ELECTED
    assert edge_b.serving(now)
    expired = override.model_copy(update={"expires_at": now - timedelta(seconds=1)})
    assert edge_b.observe(alone, now, override=expired) is ServiceRole.STANDBY


def _held(participant: StudioParticipant) -> bool:
    return participant.reservation_held


def test_hand_back_keeps_the_reservation_until_in_flight_work_finishes(tmp_path: Path) -> None:
    private, _, membership = _material()
    edge_a = _studio("coire-edge-a", private, membership, tmp_path / "lease.json")
    edge_b = _studio("coire-edge-b", private, membership, tmp_path / "b.json")
    now = datetime.now(UTC)
    view = _seen(now, "coire-edge-a", frozenset({"coire-core"}))
    edge_a.observe(view, now)
    edge_b.observe(view, now)
    grant = edge_b.vote(1, edge_a.term, "coire-edge-a", now)
    assert grant is not None and edge_a.receive_grant(grant, now)
    assert edge_a.observe(view, now) is ServiceRole.ELECTED
    assert edge_a.reservation_held
    recovered = _seen(now, "coire-edge-a", frozenset())
    recovered[0] = MemberObservation(
        name="coire-core", health=MemberHealth.HEALTHY, since=now - timedelta(seconds=30)
    )
    assert edge_a.observe(recovered, now, in_flight=2) is ServiceRole.DRAINING
    assert _held(edge_a)
    assert not edge_a.proof_path.exists()
    assert edge_a.observe(recovered, now, in_flight=0) is ServiceRole.STANDBY
    assert not _held(edge_a)
    journal = ElectionJournal(tmp_path / "coire-edge-a.json")
    assert [entry.kind.value for entry in journal.entries()] == ["promoted", "draining", "handback"]
