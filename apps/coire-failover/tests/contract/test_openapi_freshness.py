"""The Studio web client uses a checked-in schema from the failover service."""

from __future__ import annotations

from coire_failover.openapi import OUTPUT, rendered


def test_failover_openapi_is_fresh() -> None:
    assert OUTPUT.read_text(encoding="utf-8") == rendered()
