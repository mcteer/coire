"""Shared fixtures for the control-plane tests."""

from __future__ import annotations

import os

import pytest


class GatewayFakeResult:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = rows or []

    def scalars(self) -> GatewayFakeResult:
        return self

    def all(self) -> list[object]:
        return self.rows


class GatewayFakeSession:
    async def execute(self, *_: object, **__: object) -> GatewayFakeResult:
        return GatewayFakeResult()


@pytest.fixture
def gateway_fake_session() -> GatewayFakeSession:
    return GatewayFakeSession()


@pytest.fixture(autouse=True, scope="session")
def _silence_telemetry() -> None:
    """No collector exists in a unit-test run.

    Without this the OTLP exporter retries in the background and floods the output with
    connection errors that look like failures. Export itself is asserted by the integration
    suite, which runs against a real collector.
    """
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
