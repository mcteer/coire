"""Image telemetry cannot turn user content or IDs into metric labels."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from coire_api.images import telemetry as api_telemetry
from coire_node import metrics as node_metrics

ROOT = Path(__file__).resolve().parents[4]


class CounterSpy:
    def __init__(self) -> None:
        self.attributes: list[dict[str, str]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        assert amount == 1
        self.attributes.append(attributes)


def test_api_labels_are_fixed_and_content_free(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    counter = CounterSpy()
    monkeypatch.setattr(api_telemetry, "requests_total", counter)
    api_telemetry.record_image_request(
        api_telemetry.ImageOperation.SUBMIT,
        api_telemetry.ImageOutcome.REFUSED,
        reason=api_telemetry.ImageReason.QUOTA,
        job_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    assert counter.attributes == [{"operation": "submit", "outcome": "refused", "reason": "quota"}]
    with pytest.raises(ValueError):
        api_telemetry.record_image_request("attacker-provided", api_telemetry.ImageOutcome.FAILED)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        api_telemetry.record_image_request(
            api_telemetry.ImageOperation.SUBMIT,
            api_telemetry.ImageOutcome.REFUSED,
            reason="prompt text",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        api_telemetry.record_image_request(
            api_telemetry.ImageOperation.SUBMIT,
            api_telemetry.ImageOutcome.REFUSED,
            job_id="prompt text",
        )
    assert len(counter.attributes) == 1
    assert "prompt text" not in caplog.text


def test_node_labels_are_fixed_and_content_free(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = CounterSpy()
    monkeypatch.setattr(node_metrics, "image_stages_total", counter)
    node_metrics.record_image_stage(
        node_metrics.ImageNodeStage.GENERATE,
        node_metrics.ImageNodeOutcome.FAILED,
    )
    assert counter.attributes == [{"stage": "generate", "outcome": "failed"}]
    with pytest.raises(ValueError):
        node_metrics.record_image_stage("a user prompt", node_metrics.ImageNodeOutcome.FAILED)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        node_metrics.record_image_stage(
            node_metrics.ImageNodeStage.GENERATE,
            node_metrics.ImageNodeOutcome.FAILED,
            job_id="prompt text",
        )


def test_image_dashboard_and_alert_are_provisioned() -> None:
    dashboard = json.loads(
        (ROOT / "deploy/observability/grafana/dashboards/image.json").read_text()
    )
    expressions = [target["expr"] for panel in dashboard["panels"] for target in panel["targets"]]
    assert any("coire_image_requests_total" in expression for expression in expressions)
    assert any("coire_image_purge_oldest_seconds" in expression for expression in expressions)
    assert any("coire_image_input_cleanup_total" in expression for expression in expressions)
    assert any("coire_image_input_purge_oldest_seconds" in expression for expression in expressions)
    alerts: dict[str, Any] = yaml.safe_load(
        (ROOT / "deploy/observability/alerts/image.yaml").read_text()
    )
    names = {rule["alert"] for group in alerts["groups"] for rule in group["rules"]}
    assert {
        "CoireImageFailures",
        "CoireImagePurgeOverdue",
        "CoireImageInputCleanupFailures",
        "CoireImageInputPurgeOverdue",
    } <= names
    assert "alerts/image.yaml" in (ROOT / "deploy/compose/prometheus.Dockerfile").read_text()
    assert "dashboards/image.json" in (ROOT / "deploy/compose/grafana.Dockerfile").read_text()
