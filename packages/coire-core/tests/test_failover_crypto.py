"""Prepared keys must preserve per-message verification and rotation boundaries."""

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coire_core.failover_crypto import (
    Ed25519Signer,
    public_key_b64,
    sign_ed25519,
    verify_ed25519,
)


def _key() -> str:
    return base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode()


def test_prepared_signer_verifies_each_payload_and_key_rotation() -> None:
    original, rotated = _key(), _key()
    signer = Ed25519Signer(original)
    public, next_public = public_key_b64(original), public_key_b64(rotated)
    for payload in (b"first challenge", b"second challenge", b"first challenge"):
        signature = signer.sign(payload)
        assert signature == sign_ed25519(payload, original)
        assert verify_ed25519(payload, signature, public)
        assert not verify_ed25519(payload + b"tampered", signature, public)
        assert not verify_ed25519(payload, signature, next_public)
        replacement = Ed25519Signer(rotated).sign(payload)
        assert verify_ed25519(payload, replacement, next_public)
        assert not verify_ed25519(payload, replacement, public)


@pytest.mark.parametrize("malformed", ["not base64!", base64.b64encode(b"short").decode()])
def test_malformed_keys_and_signatures_still_fail_closed(malformed: str) -> None:
    key = _key()
    signature = Ed25519Signer(key).sign(b"payload")
    assert not verify_ed25519(b"payload", malformed, public_key_b64(key))
    assert not verify_ed25519(b"payload", signature, malformed)
    with pytest.raises(ValueError):
        Ed25519Signer(malformed)
