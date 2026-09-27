from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

from pydantic import SecretStr

from coire_api.db import NodeRow
from coire_api.instance.registration import issue_token, verify_token
from coire_core.settings import Settings


def _settings() -> Settings:
    return Settings(
        _secrets_dir="/nonexistent",  # type: ignore[call-arg]
        key_signing_secret=SecretStr("test-signing-key"),
    )


def _row() -> NodeRow:
    return cast(
        NodeRow,
        SimpleNamespace(
            id=uuid.uuid4(),
            registration_token_digest=None,
            token_issued_at=None,
            token_consumed_at=None,
            token_revoked_at=None,
        ),
    )


def test_issued_token_is_stored_only_as_digest_and_is_single_use() -> None:
    row = _row()
    credential = issue_token(row, _settings())
    assert credential.token not in (row.registration_token_digest or "")
    assert verify_token(row, credential.token, _settings()) == (True, "accepted")
    row.token_consumed_at = credential.issued_at
    assert verify_token(row, credential.token, _settings()) == (False, "consumed")


def test_wrong_and_revoked_tokens_are_refused() -> None:
    row = _row()
    credential = issue_token(row, _settings())
    assert verify_token(row, credential.token + "x", _settings()) == (False, "invalid")
    row.token_revoked_at = credential.issued_at
    assert verify_token(row, credential.token, _settings()) == (False, "revoked")
