"""Control-plane telemetry safety tests."""

import logging

from coire_api.telemetry import TelemetryRedactionFilter


def test_sensitive_structured_fields_are_redacted() -> None:
    record = logging.makeLogRecord(
        {"name": "coire.api", "msg": "safe", "authorization": "Bearer unsafe", "job_id": "kept"}
    )
    assert TelemetryRedactionFilter().filter(record)
    assert record.authorization == "[REDACTED]"  # type: ignore[attr-defined]
    assert record.job_id == "kept"  # type: ignore[attr-defined]


def test_exporter_retry_logs_are_not_reexported() -> None:
    record = logging.makeLogRecord({"name": "opentelemetry.exporter.otlp", "msg": "retry"})
    assert not TelemetryRedactionFilter().filter(record)
