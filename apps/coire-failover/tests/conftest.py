"""Explicit isolated route identities; production still requires independently sourced secrets."""

from collections.abc import Iterator

import pytest

from coire_core.settings import get_settings


@pytest.fixture(autouse=True)
def failover_test_identities(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FAILOVER_MEMBER_NAME", "coire-edge-a")
    monkeypatch.setenv("FAILOVER_MONITOR_TOKEN", "isolated-monitor")
    monkeypatch.setenv("FAILOVER_LIFECYCLE_TOKEN", "isolated-lifecycle")
    monkeypatch.setenv("FAILOVER_RELAY_TOKEN", "isolated-relay")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
