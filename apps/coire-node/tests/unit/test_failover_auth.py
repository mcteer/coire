import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coire_core.failover_crypto import sign_ed25519
from coire_core.models.failover import ElectionVoteGrant, FailoverEvent, FailoverEventKind
from coire_node.failover.auth import verify_relay_credential, verify_vote_grant
from coire_node.failover.journal import ElectionJournal


def test_relay_credential_is_scoped_and_fails_closed() -> None:
    assert verify_relay_credential("relay-secret", "relay-secret")
    assert not verify_relay_credential(None, "relay-secret")
    assert not verify_relay_credential("wrong", "relay-secret")


def test_vote_grant_signature_rejects_tampering() -> None:
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
    unsigned = ElectionVoteGrant(
        epoch=1,
        term=2,
        candidate="coire-edge-a",
        voter="coire-core",
        expires_at=datetime.now(UTC) + timedelta(seconds=15),
        signature="unsigned",
    )
    signed = unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), private_b64)}
    )
    assert verify_vote_grant(signed, public_b64)
    assert not verify_vote_grant(signed.model_copy(update={"term": 3}), public_b64)


def test_journal_is_bounded_and_idempotent_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    journal = ElectionJournal(path, capacity=2)
    events = [
        FailoverEvent(
            event_id=uuid4(),
            term=index + 1,
            kind=FailoverEventKind.PROMOTED,
            host="coire-edge-a",
            occurred_at=datetime.now(UTC),
            proof_digest="a" * 64,
        )
        for index in range(3)
    ]
    for event in events:
        journal.append(event)
    ElectionJournal(path, capacity=2).append(events[-1])
    assert [entry.event_id for entry in ElectionJournal(path, capacity=2).entries()] == [
        events[1].event_id,
        events[2].event_id,
    ]
