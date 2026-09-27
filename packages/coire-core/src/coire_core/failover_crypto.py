"""Ed25519 helpers for the failover trust boundary.

Only public verification keys and signatures travel in failover snapshots. Private keys remain in
Keychain-backed file secrets on the member that signs a message.
"""

from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def public_key_b64(private_key_b64: str) -> str:
    """Derive the raw base64 public key that peers store in membership."""
    private_key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(private_key_b64, validate=True)
    )
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def sign_ed25519(payload: bytes, private_key_b64: str) -> str:
    """Return an RFC 4648 base64 Ed25519 signature for canonical ``payload``."""
    private_key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(private_key_b64, validate=True)
    )
    return base64.b64encode(private_key.sign(payload)).decode("ascii")


def verify_ed25519(payload: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """Verify a base64 signature, returning false for malformed or invalid data."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64, validate=True)
        )
        public_key.verify(base64.b64decode(signature_b64, validate=True), payload)
    except (InvalidSignature, ValueError):
        return False
    return True
