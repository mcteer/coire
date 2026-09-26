"""Hand-back drain, recovery damping, and journal reconciliation."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from coire_api.failover import reconcile
from coire_api.failover.participant import CoreParticipant
from coire_api.failover.poller import CoreFailoverPoller
from coire_core.failover_crypto import sign_ed25519
from coire_core.failover_election import (
    ElectionParticipant,
    ElectionTimings,
    MemberHealth,
    MemberObservation,
    ServiceRole,
)
from coire_core.failover_poller import FailoverPoller
from coire_core.models.failover import (
    FailoverEvent,
    FailoverEventKind,
    FailoverMember,
    FailoverMembershipConfig,
    HandbackNotice,
)
from coire_core.settings import Settings
from coire_node.failover.participant import StudioParticipant


def _pair(tmp_path: Path) -> tuple[StudioParticipant, ElectionParticipant, str, datetime]:
    keys: dict[str, str] = {}
    publics: dict[str, str] = {}
    for name in ("coire-core", "coire-edge-a", "coire-edge-b"):
        key = Ed25519PrivateKey.generate()
        keys[name] = base64.b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode()
        publics[name] = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode()
    membership = FailoverMembershipConfig(
        epoch=1,
        signing_key_id="core-1",
        members=[
            FailoverMember(name=name, priority=index, public_key=publics[name])
            for index, name in enumerate(("coire-core", "coire-edge-a", "coire-edge-b"))
        ],
    )
    timings = ElectionTimings(
        promotion=timedelta(seconds=10),
        demotion=timedelta(seconds=30),
        lease=timedelta(minutes=5),
        drain=timedelta(seconds=20),
    )
    edge_a = StudioParticipant(
        name="coire-edge-a",
        membership=membership,
        private_key_b64=keys["coire-edge-a"],
        timings=timings,
        proof_path=tmp_path / "lease.json",
    )
    edge_b = ElectionParticipant(
        name="coire-edge-b",
        membership=membership,
        private_key_b64=keys["coire-edge-b"],
        timings=timings,
    )
    return edge_a, edge_b, keys["coire-core"], datetime.now(UTC)


def _elect(edge_a: StudioParticipant, edge_b: ElectionParticipant, now: datetime) -> None:
    view = [
        MemberObservation(
            name=name,
            health=MemberHealth.UNREACHABLE if name == "coire-core" else MemberHealth.HEALTHY,
            since=now - timedelta(seconds=20),
        )
        for name in ("coire-core", "coire-edge-a", "coire-edge-b")
    ]
    edge_a.observe(view, now)
    edge_b.observe(view, now)
    grant = edge_b.vote(1, edge_a.term, "coire-edge-a", now)
    assert grant is not None and edge_a.receive_grant(grant, now)
    assert edge_a.observe(view, now) is ServiceRole.ELECTED


@pytest.mark.asyncio
async def test_recovered_core_sends_signed_handback_to_both_studios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge_a, _, core_key, now = _pair(tmp_path)
    settings = Settings(  # type: ignore[call-arg]
        _secrets_dir="/nonexistent",
        failover_peer_key=SecretStr(core_key),
    )
    core = CoreParticipant(
        name="coire-core",
        membership=edge_a.membership,
        private_key_b64=core_key,
        timings=edge_a.timings,
    )
    poller = CoreFailoverPoller(core, settings)
    seen: list[str] = []

    async def pretend_tick(self: FailoverPoller, moment: datetime | None = None) -> None:
        del moment
        self.participant.role = ServiceRole.CANDIDATE

    def handler(request: httpx.Request) -> httpx.Response:
        notice = HandbackNotice.model_validate_json(request.content)
        assert notice.successor == "coire-core"
        assert notice.holder in ("coire-edge-a", "coire-edge-b")
        assert request.url.path == "/node/failover/election/handback"
        assert edge_a.member_public_key("coire-core")
        from coire_core.failover_crypto import verify_ed25519

        assert verify_ed25519(
            notice.canonical_bytes(), notice.signature, edge_a.member_public_key("coire-core")
        )
        seen.append(notice.holder)
        return httpx.Response(204)

    monkeypatch.setattr(FailoverPoller, "tick", pretend_tick)
    await poller._handback_client.aclose()
    poller._handback_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await poller.tick(now)
    assert seen == ["coire-edge-a", "coire-edge-b"]
    await poller.stop()


def test_flapping_core_does_not_drain_before_the_demotion_threshold(tmp_path: Path) -> None:
    edge_a, edge_b, core_key, now = _pair(tmp_path)
    _elect(edge_a, edge_b, now)
    flap = [
        MemberObservation(
            name="coire-core", health=MemberHealth.HEALTHY, since=now - timedelta(seconds=5)
        ),
        MemberObservation(name="coire-edge-a", health=MemberHealth.HEALTHY, since=now),
        MemberObservation(name="coire-edge-b", health=MemberHealth.HEALTHY, since=now),
    ]
    assert edge_a.observe(flap, now) is ServiceRole.ELECTED
    assert edge_a.serving(now)
    unsigned = HandbackNotice(
        epoch=1,
        term=edge_a.term,
        holder="coire-edge-a",
        successor="coire-core",
        expires_at=now + timedelta(seconds=30),
        signature="unsigned",
    )
    notice = unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), core_key)}
    )
    assert edge_a.accept_handback(notice, now)
    assert edge_a.role is ServiceRole.STANDBY
    assert not edge_a.reservation_held


@pytest.mark.asyncio
async def test_reconcile_replays_a_journal_without_duplicating_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts: dict[object, object] = {}
    added_rows: list[object] = []

    class Session:
        async def get(self, _model: object, event_id: object) -> object | None:
            return receipts.get(event_id)

        def add(self, row: object) -> None:
            added_rows.append(row)
            receipts[row.event_id] = row  # type: ignore[attr-defined]

        async def flush(self) -> None:
            return None

    async def fake_write_audit(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(reconcile, "write_audit", fake_write_audit)
    event = FailoverEvent(
        event_id=uuid4(),
        term=4,
        kind=FailoverEventKind.HANDBACK,
        host="coire-edge-a",
        occurred_at=datetime.now(UTC),
        proof_digest="e" * 64,
    )
    session = Session()
    assert await reconcile.reconcile_events(session, [event, event]) == 1  # type: ignore[arg-type]
    assert await reconcile.reconcile_events(session, [event]) == 0  # type: ignore[arg-type]
    assert len(added_rows) == 1
