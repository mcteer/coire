"""Actual OTLP -> hardened collector -> Prometheus naming/cardinality gate."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from coire_api.training.telemetry import TrainingBaselineMetrics
from coire_scheduler.training_metrics import reduce_training_metrics

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="requires isolated Docker collector: COIRE_INTEGRATION=1",
    ),
]


def test_exported_names_and_bounded_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    root = Path(__file__).resolve().parents[4]
    name = "coire-training-metrics-test-" + uuid.uuid4().hex[:12]
    provider: MeterProvider | None = None
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-p",
            "127.0.0.1::4317",
            "-p",
            "127.0.0.1::8889",
            "--mount",
            f"type=bind,src={root / 'deploy/observability/tests/training-collector.yaml'},dst=/test.yaml,readonly",
            "coire-otel:dev",
            "--config=/test.yaml",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    try:

        def port(internal: int) -> str:
            return (
                subprocess.check_output(
                    ["docker", "port", name, f"{internal}/tcp"], text=True, timeout=10
                )
                .strip()
                .rsplit(":", 1)[1]
            )

        grpc_port, prometheus_port = port(4317), port(8889)
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"http://127.0.0.1:{grpc_port}", insecure=True),
            export_interval_millis=60000,
        )
        provider = MeterProvider(metric_readers=[reader])
        emitter = TrainingBaselineMetrics(provider.get_meter("test.training"))
        emitter.publish(
            reduce_training_metrics(
                now=datetime.now(UTC),
                jobs={"running": 1},
                attempts=[],
                recoveries=[],
                pending_checkpoints=[],
            )
        )
        deadline = time.monotonic() + 20
        exported = ""
        while time.monotonic() < deadline:
            provider.force_flush(timeout_millis=2000)
            try:
                response = httpx.get(f"http://127.0.0.1:{prometheus_port}/metrics", timeout=2)
                response.raise_for_status()
                exported = response.text
                if "coire_training_snapshot_timestamp_seconds" in exported:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        for metric in (
            "snapshot_timestamp_seconds",
            "jobs",
            "progress_oldest_seconds",
            "recovery_oldest_seconds",
            "checkpoint_pending_oldest_seconds",
            "guard_overdue",
        ):
            assert "coire_training_" + metric in exported
        samples = [line for line in exported.splitlines() if line.startswith("coire_training_")]
        assert len(samples) == 21  # 12 state + 5 reason + 4 scalar series
        assert all(
            forbidden not in line
            for line in samples
            for forbidden in (
                "job_id",
                "attempt_id",
                "model_id",
                "user_id",
                "dataset_id",
                "adapter_id",
            )
        )
        assert any('state="running"' in line and line.split()[-1] == "1" for line in samples)
    finally:
        if provider is not None:
            provider.shutdown()
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30)
