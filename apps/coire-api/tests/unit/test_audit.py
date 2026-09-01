"""Audit redaction (T016)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock

from coire_api.audit import redact, write_audit
from coire_api.auth import bind_request_id, reset_request_id


class TestRedaction:
    def test_credential_shaped_keys_are_dropped(self) -> None:
        out = redact(
            {
                "token": "hf_secret",
                "node_token": "abc",
                "api_secret": "s",
                "password": "p",
                "transfer_grant": "g",
                "Authorization": "Bearer x",
            }
        )
        assert set(out.values()) == {"[redacted]"}
        assert "hf_secret" not in str(out)

    def test_ordinary_keys_survive(self) -> None:
        out = redact({"repo_id": "mlx-community/x", "bytes": 12, "ok": True})
        assert out == {"repo_id": "mlx-community/x", "bytes": 12, "ok": True}

    def test_nested_dictionaries_are_redacted(self) -> None:
        """Node responses arrive nested; a top-level-only scan would miss them."""
        out = redact({"node": {"name": "coire-edge-a", "token": "leak"}})
        assert out["node"]["token"] == "[redacted]"
        assert out["node"]["name"] == "coire-edge-a"

    def test_long_values_are_truncated(self) -> None:
        """An audit row is not a log sink; a 4 MiB traceback does not belong in one."""
        out = redact({"error": "x" * 5000})
        assert len(str(out["error"])) < 600

    def test_credential_values_are_sanitized_even_under_innocent_keys(self) -> None:
        key = "coire_abcdefghijkl_" + "s" * 43
        jwt = "eyJ" + "a" * 30 + "." + "b" * 20 + "." + "c" * 20
        out = redact({"message": f"failed {key}", "items": [jwt, {"secret": "leak"}]})
        assert key not in str(out)
        assert jwt not in str(out)
        assert str(out).count("[redacted]") == 3

    def test_empty_and_none_are_empty(self) -> None:
        assert redact(None) == {}
        assert redact({}) == {}


async def test_audit_writer_projects_bound_request_identity() -> None:
    session = Mock()
    session.add = Mock()
    session.flush = AsyncMock()
    request_id = uuid.uuid4()
    token = bind_request_id(request_id)
    try:
        row = await write_audit(
            session,
            actor="service",
            action="test.action",
            target_type="test",
            target_id="one",
        )
    finally:
        reset_request_id(token)
    assert row.request_id == request_id
