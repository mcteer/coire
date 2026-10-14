"""Real local SDK telemetry at a refused stop gate; no command/Core effects."""

import logging
from datetime import UTC, timedelta
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from test_upgrade_recovery_owner_claim_contract import owner_claim_payload

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryOwnerClaim
from coire_core.settings import Settings


def test_stop_budget_actual_sdk_metric_span_and_scoped_log(tmp_path, monkeypatch, caplog):
    from coire_node import expired_recovery_stop as worker

    owner = UpgradeExpiredRecoveryOwnerClaim.model_validate(owner_claim_payload(tmp_path))
    receipt = owner.consumption
    expired = receipt.transfer.expires_at + timedelta(seconds=1)

    class Clock:
        @staticmethod
        def now(tz=UTC):
            return expired

    monkeypatch.setattr(worker, "datetime", Clock)
    # API fixtures disable export globally during full-suite collection. This
    # test explicitly measures its isolated local SDK providers, never network
    # export; restore the caller's setting automatically after the test.
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    exporter = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(worker, "tracer", traces.get_tracer("test.stop_budget"))
    monkeypatch.setattr(
        worker,
        "stop_budget_events",
        provider.get_meter("test.stop_budget").create_counter(
            "coire_upgrade_recovery_stop_budget_total"
        ),
    )
    control = Mock()
    command = Mock()
    monkeypatch.setattr(worker, "capture_owned_command", command)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.json")}
    try:
        with (
            caplog.at_level(logging.WARNING, logger=worker.__name__),
            pytest.raises(UpgradeUnavailable, match="captured budget expired"),
        ):
            worker.stop_legacy_guard(Settings(), receipt, owner.runtime, control)
        control.verify_recovery_consumption.assert_not_called()
        command.assert_not_called()
        assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.json")}
        data = reader.get_metrics_data()
        measurements = [
            metric
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        ]
        assert len(measurements) == 1
        metric = measurements[0]
        assert metric.name == "coire_upgrade_recovery_stop_budget_total"
        assert len(metric.data.data_points) == 1
        point = metric.data.data_points[0]
        assert point.value == 1
        assert dict(point.attributes) == {"outcome": "refused"}
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "coire.node.recovery_legacy_stop_budget"
        assert spans[0].status.status_code == StatusCode.ERROR
        record = next(r for r in caplog.records if r.name == worker.__name__)
        assert record.job_id == receipt.transfer.intent.request.original_job_id
        assert record.recovery_id == receipt.transfer.intent.recovery_id
        assert record.generation == receipt.transfer.intent.original_plan.operation.generation
        assert record.exc_info is None
    finally:
        traces.shutdown()
        provider.shutdown()
