"""Operator overrides expire and a bad signature changes nothing."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from coire_api.failover.overrides import OverrideRejected, OverrideStore
from coire_core.failover_crypto import public_key_b64, sign_ed25519
from coire_core.models.failover import FailoverOverride, FailoverOverrideKind


def _signed(private: str, *, expires: datetime) -> FailoverOverride:
    unsigned = FailoverOverride(
        kind=FailoverOverrideKind.INHIBIT,
        actor_id=uuid4(),
        reason="hold promotion",
        expires_at=expires,
        signature="unsigned",
    )
    return unsigned.model_copy(
        update={"signature": sign_ed25519(unsigned.canonical_bytes(), private)}
    )


def test_override_expires_and_rejects_a_bad_signature() -> None:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()
    public = public_key_b64(private)
    store = OverrideStore()
    now = datetime.now(UTC)
    applied = store.apply(_signed(private, expires=now + timedelta(seconds=30)), public, now)
    assert store.active(now) == applied
    assert store.active(now + timedelta(seconds=31)) is None
    forged = applied.model_copy(update={"reason": "forged"})
    with pytest.raises(OverrideRejected):
        store.apply(forged, public, now)
