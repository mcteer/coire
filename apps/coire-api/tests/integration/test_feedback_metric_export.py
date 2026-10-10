"""Actual baseline feedback names through a diagnostics-free OTLP collector."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from coire_api.feedback import baseline, retention

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("COIRE_INTEGRATION") != "1",
        reason="requires isolated Docker collector: COIRE_INTEGRATION=1",
    ),
]


def test_feedback_exported_names_and_bounded_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    root = Path(__file__).resolve().parents[4]
    name = "coire-feedback-metrics-test-" + uuid.uuid4().hex[:12]
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
            f"coire-otel:{os.environ.get('COIRE_TAG', 'dev')}",
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
        meter = provider.get_meter("test.feedback")
        for field, instrument in (
            ("export_states", "coire_feedback_exports"),
            ("export_overdue", "coire_feedback_export_overdue"),
            ("export_cleanup_pending", "coire_feedback_export_cleanup_pending"),
            ("snapshot_timestamp", "coire_feedback_snapshot_timestamp"),
        ):
            monkeypatch.setattr(
                baseline,
                field,
                meter.create_gauge(instrument, unit="s" if field == "snapshot_timestamp" else ""),
            )
        monkeypatch.setattr(
            retention,
            "purge_oldest_seconds",
            meter.create_gauge("coire_feedback_purge_oldest_seconds", unit="s"),
        )
        session = AsyncMock()
        rows = MagicMock()
        rows.tuples.return_value.all.return_value = [("queued", 2)]
        session.execute.return_value = rows
        session.scalar.side_effect = [1, 0, None, None, None]
        asyncio.run(baseline.record_feedback_metrics(session))
        deadline = time.monotonic() + 20
        exported = ""
        while time.monotonic() < deadline:
            provider.force_flush(timeout_millis=2000)
            try:
                response = httpx.get(f"http://127.0.0.1:{prometheus_port}/metrics", timeout=2)
                response.raise_for_status()
                exported = response.text
                if "coire_feedback_snapshot_timestamp_seconds" in exported:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        for metric in (
            "snapshot_timestamp_seconds",
            "exports",
            "purge_oldest_seconds",
            "export_overdue",
            "export_cleanup_pending",
        ):
            assert "coire_feedback_" + metric in exported
        samples = [line for line in exported.splitlines() if line.startswith("coire_feedback_")]
        assert len(samples) == 10  # six fixed states and four scalar series
        assert all(
            forbidden not in line
            for line in samples
            for forbidden in (
                "job_id",
                "user_id",
                "pair_id",
                "prompt",
                "candidate",
                "tag=",
                "dataset_id",
            )
        )
        assert any('state="queued"' in line and line.split()[-1] == "2" for line in samples)
        assert any('state="failed"' in line and line.split()[-1] == "0" for line in samples)
    finally:
        if provider is not None:
            provider.shutdown()
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30)
