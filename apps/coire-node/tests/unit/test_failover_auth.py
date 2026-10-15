import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coire_core.failover_crypto import sign_ed25519
from coire_core.models.failover import ElectionVoteGrant, FailoverEvent, FailoverEventKind
from coire_node.failover.auth import verify_relay_credential, verify_vote_grant
from coire_node.failover.journal import ElectionJournal, ElectionJournalError


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
        issued_at=datetime.now(UTC),
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
    for event in events[:2]:
        journal.append(event)
    with pytest.raises(ElectionJournalError, match="full"):
        journal.append(events[2])
    ElectionJournal(path, capacity=2).append(events[1])
    assert [entry.event_id for entry in ElectionJournal(path, capacity=2).entries()] == [
        events[0].event_id,
        events[1].event_id,
    ]
    assert not journal.admission_available()
    terminal = events[2].model_copy(update={"kind": FailoverEventKind.FENCED})
    journal.append(terminal)
    assert {entry.event_id for entry in journal.entries()} == {event.event_id for event in events}
    journal.acknowledge([events[0].event_id])
    assert journal.admission_available()
    assert {entry.event_id for entry in journal.entries()} == {
        events[1].event_id,
        events[2].event_id,
    }


def test_corrupt_ordinary_journal_preserves_reserved_fence_record(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    path.write_text("corrupt ordinary evidence", encoding="utf-8")
    journal = ElectionJournal(path)
    assert not journal.admission_available()
    event = FailoverEvent(
        term=1,
        kind=FailoverEventKind.FENCED,
        host="coire-edge-a",
        occurred_at=datetime.now(UTC),
        proof_digest="b" * 64,
    )
    journal.append(event)
    assert path.read_text(encoding="utf-8") == "corrupt ordinary evidence"
    assert event.event_id in {
        item.event_id for item in journal._read(path.with_name("journal.json.terminal"), 2)
    }


def test_unknown_acknowledgement_never_discards_evidence(tmp_path: Path) -> None:
    journal = ElectionJournal(tmp_path / "journal.json")
    event = FailoverEvent(
        term=1,
        kind=FailoverEventKind.PROMOTED,
        host="coire-edge-a",
        occurred_at=datetime.now(UTC),
        proof_digest="b" * 64,
    )
    journal.append(event)
    with pytest.raises(ElectionJournalError, match="unknown"):
        journal.acknowledge([uuid4()])
    assert journal.entries() == [event]


def test_oversized_journal_fails_closed_before_decoding(tmp_path: Path) -> None:
    path = tmp_path / "journal.json"
    path.write_bytes(b" " * (ElectionJournal._max_bytes + 1))
    journal = ElectionJournal(path)
    assert not journal.admission_available()
    with pytest.raises(ElectionJournalError):
        journal.entries()
