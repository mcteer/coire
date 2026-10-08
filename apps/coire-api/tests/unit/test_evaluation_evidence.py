"""Private generated evidence paths and immutable content-addressed receipts."""

import hashlib
import uuid
from pathlib import Path

import pytest

from coire_api.evaluation.evidence import EvidenceStore
from coire_core.errors import EvaluationConflict, EvaluationValidationError
from coire_core.settings import Settings


async def test_evidence_atomic_replay_and_digest_validation(tmp_path: Path) -> None:
    store = EvidenceStore(Settings(training_dataset_dir=str(tmp_path)))
    identity = uuid.uuid4()
    payload = b'{"outputs":[]}'
    digest = hashlib.sha256(payload).hexdigest()
    assert await store.stage(identity, payload, digest) == len(payload)
    assert await store.read(identity, digest) == payload
    assert await store.stage(identity, payload, digest) == len(payload)
    with pytest.raises(EvaluationValidationError):
        await store.stage(uuid.uuid4(), payload, "0" * 64)
    with pytest.raises(EvaluationConflict):
        await store.stage(identity, b"changed", hashlib.sha256(b"changed").hexdigest())
    await store.remove(identity)
    await store.remove(identity)


async def test_evidence_symlink_and_hardlink_refusal(tmp_path: Path) -> None:
    store = EvidenceStore(Settings(training_dataset_dir=str(tmp_path)))
    identity = uuid.uuid4()
    payload = b"secret"
    original = tmp_path / "private"
    original.write_bytes(payload)
    path = store.root / str(identity)
    path.symlink_to(original)
    with pytest.raises(EvaluationValidationError):
        await store.read(identity, hashlib.sha256(payload).hexdigest())
    path.unlink()
    path.hardlink_to(original)
    with pytest.raises(EvaluationValidationError):
        await store.read(identity, hashlib.sha256(payload).hexdigest())


async def test_evidence_bytes_have_a_hard_bound(tmp_path: Path) -> None:
    store = EvidenceStore(Settings(training_dataset_dir=str(tmp_path)))
    payload = b"x" * (8 * 1024 * 1024 + 1)
    with pytest.raises(EvaluationValidationError):
        await store.stage(uuid.uuid4(), payload, hashlib.sha256(payload).hexdigest())
