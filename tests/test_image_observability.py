"""Image alerts stay provisioned in lean mode and omit private content labels."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "deploy/observability/alerts/image.yaml"
DASHBOARD = ROOT / "deploy/observability/grafana/dashboards/image.json"


def test_image_rules_are_in_the_baseline_prometheus_image() -> None:
    dockerfile = (ROOT / "deploy/compose/prometheus.Dockerfile").read_text()
    config = yaml.safe_load((ROOT / "deploy/compose/prometheus/prometheus.yml").read_text())
    compose = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    assert (
        "COPY deploy/observability/alerts/image.yaml /etc/prometheus/rules/image.yml" in dockerfile
    )
    assert "/etc/prometheus/rules/*.yml" in config["rule_files"]
    for service in ("otel-collector", "prometheus", "alertmanager"):
        assert "profiles" not in compose["services"][service]
    for service in ("loki", "tempo", "grafana"):
        assert "diagnostics" in compose["services"][service]["profiles"]


def test_image_dashboard_and_alerts_have_actionable_content_free_queries() -> None:
    rules = yaml.safe_load(ALERTS.read_text())["groups"][0]["rules"]
    dashboard = json.loads(DASHBOARD.read_text())
    grafanafile = (ROOT / "deploy/compose/grafana.Dockerfile").read_text()
    assert "COPY deploy/observability/grafana/dashboards/image.json" in grafanafile
    names = {rule["alert"] for rule in rules}
    assert len(names) == len(rules)
    assert {
        "CoireImageObservationFailures",
        "CoireImageAuthorizationRevocations",
        "CoireImageClassifierFailures",
        "CoireImageCancellationRecoveryFailures",
        "CoireImageOutputCleanupFailures",
        "CoireImageInputCleanupFailures",
        "CoireImageQueueReconciliationFailures",
        "CoireImageDispatchUncertain",
        "CoireImageWorkerStopFailures",
        "CoireImageStorageFailures",
        "CoireImageChatRegression",
    } <= names
    assert dashboard["panels"]
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert {
        "Image placement and queue reconciliation",
        "Image cancellation and worker stops",
        "Image storage refusals",
        "Studio image cache occupancy",
        "Studio image classification",
        "Gateway chat first-token p95 during image activity",
    } <= titles
    queries = [rule["expr"] for rule in rules]
    queries.extend(
        target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])
    )
    assert all("coire_image_" in query for query in queries)
    assert all(
        "coire_image_dispatch_total" in query
        for query in queries
        if "coire_gateway_first_token_duration_ms" in query
    )
    assert all(
        secret not in query.lower()
        for query in queries
        for secret in ("prompt", "owner_id", "user_id", "grant", "image_id")
    )
    assert all("token=" not in query and "token=~" not in query for query in queries)
