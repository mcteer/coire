"""Narrow authentication checks for signed election traffic and Studio relay requests."""

from __future__ import annotations

import hmac

from coire_core.failover_crypto import verify_ed25519
from coire_core.models.failover import ElectionVoteGrant


def verify_vote_grant(grant: ElectionVoteGrant, voter_public_key: str) -> bool:
    """Authenticate an election grant independently of node-registration credentials."""
    return verify_ed25519(grant.canonical_bytes(), grant.signature, voter_public_key)


def verify_relay_credential(presented: str | None, expected: str) -> bool:
    """Constant-time check for the separately scoped local inference-relay credential."""
    return bool(presented and expected and hmac.compare_digest(presented, expected))
