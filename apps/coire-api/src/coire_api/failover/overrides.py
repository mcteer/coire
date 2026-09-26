"""Expiring operator overrides. A bad signature never changes election behaviour."""

from __future__ import annotations

from datetime import UTC, datetime

from coire_core.failover_crypto import verify_ed25519
from coire_core.models.failover import FailoverOverride


class OverrideRejected(ValueError):
    """The override is expired or its core signature does not verify."""


class OverrideStore:
    """The single active override. Expiry is checked on every read."""

    def __init__(self) -> None:
        self._current: FailoverOverride | None = None

    def active(self, now: datetime | None = None) -> FailoverOverride | None:
        current = self._current
        if current is not None and current.is_current(now or datetime.now(UTC)):
            return current
        self._current = None
        return None

    def apply(
        self, override: FailoverOverride, public_key: str, now: datetime | None = None
    ) -> FailoverOverride:
        moment = now or datetime.now(UTC)
        if not override.is_current(moment):
            raise OverrideRejected("override is expired")
        if not verify_ed25519(override.canonical_bytes(), override.signature, public_key):
            raise OverrideRejected("override signature is invalid")
        self._current = override
        return override


_store = OverrideStore()


def get_override_store() -> OverrideStore:
    return _store
