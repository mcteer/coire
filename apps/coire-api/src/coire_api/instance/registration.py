"""One-time declared-node registration credentials."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from coire_api.db import NodeRow
from coire_core.models.instance import NodeRegistrationCredential
from coire_core.settings import Settings


def token_digest(token: str, settings: Settings) -> str:
    key = settings.key_signing_secret.get_secret_value().encode()
    if not key:
        raise RuntimeError("key signing secret is required to issue node credentials")
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def issue_token(row: NodeRow, settings: Settings) -> NodeRegistrationCredential:
    token = secrets.token_urlsafe(32)
    issued_at = datetime.now(UTC)
    row.registration_token_digest = token_digest(token, settings)
    row.token_issued_at = issued_at
    row.token_consumed_at = None
    row.token_revoked_at = None
    return NodeRegistrationCredential(node_id=row.id, token=token, issued_at=issued_at)


def verify_token(row: NodeRow, token: str, settings: Settings) -> tuple[bool, str]:
    if row.token_revoked_at is not None:
        return False, "revoked"
    if row.token_consumed_at is not None:
        return False, "consumed"
    if row.registration_token_digest is None:
        return False, "not_issued"
    presented = token_digest(token, settings)
    if not hmac.compare_digest(row.registration_token_digest, presented):
        return False, "invalid"
    return True, "accepted"
