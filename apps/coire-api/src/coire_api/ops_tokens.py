"""Opaque, memory-hard, exact-action confirmation tokens."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from coire_core.models.ops import ResolvedOpsAction

CONFIRM_TOKEN_PATTERN = re.compile(r"^coire_confirm_([A-Za-z0-9_-]{12})_([A-Za-z0-9_-]{43})$")
hasher = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)


class InvalidConfirmation(ValueError):
    """A bounded, client-safe confirmation refusal."""

    ALLOWED_REASONS = frozenset(
        {
            "malformed",
            "unknown",
            "secret_mismatch",
            "action_mismatch",
            "expired",
            "used",
            "revoked",
            "stale",
            "session_restarted",
            "not_pending",
        }
    )

    def __init__(self, reason: str) -> None:
        if reason not in self.ALLOWED_REASONS:
            reason = "unknown"
        self.reason = reason
        super().__init__("confirmation refused")


def token_material() -> tuple[str, str, str]:
    prefix = secrets.token_urlsafe(9)
    secret = secrets.token_urlsafe(32)
    return prefix, secret, f"coire_confirm_{prefix}_{secret}"


def hash_secret(secret: str) -> str:
    return hasher.hash(secret)


def parse_token(presented: str) -> tuple[str, str]:
    match = CONFIRM_TOKEN_PATTERN.fullmatch(presented)
    if match is None:
        raise InvalidConfirmation("malformed")
    return match.group(1), match.group(2)


def verify_secret(secret_hash: str, presented_secret: str) -> bool:
    try:
        return bool(hasher.verify(secret_hash, presented_secret))
    except VerificationError:
        return False


def canonical_action_digest(
    *,
    proposal_id: uuid.UUID,
    conversation_id: uuid.UUID,
    session_id: uuid.UUID,
    action: ResolvedOpsAction,
) -> str:
    payload = {
        "proposal_id": str(proposal_id),
        "conversation_id": str(conversation_id),
        "ops_session_id": str(session_id),
        "action": action.model_dump(mode="json", exclude_none=False),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
