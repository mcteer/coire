"""Node telemetry safety tests."""

import logging

from coire_node.otel import TelemetryRedactionFilter


def test_sensitive_structured_fields_are_redacted() -> None:
    record = logging.makeLogRecord(
        {"name": "coire.node", "msg": "safe", "node_token": "not-safe", "run_id": "kept"}
    )
    assert TelemetryRedactionFilter().filter(record)
    assert record.node_token == "[REDACTED]"  # type: ignore[attr-defined]
    assert record.run_id == "kept"  # type: ignore[attr-defined]


def test_exporter_retry_logs_are_not_reexported() -> None:
    record = logging.makeLogRecord({"name": "opentelemetry.exporter.otlp", "msg": "retry"})
    assert not TelemetryRedactionFilter().filter(record)
