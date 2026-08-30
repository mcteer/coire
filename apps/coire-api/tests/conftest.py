"""Shared fixtures for the control-plane tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _silence_telemetry() -> None:
    """No collector exists in a unit-test run.

    Without this the OTLP exporter retries in the background and floods the output with
    connection errors that look like failures. Export itself is asserted by the integration
    suite, which runs against a real collector.
    """
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
