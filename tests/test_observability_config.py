from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_complete_alert_set_is_named_and_actionable() -> None:
    doc = yaml.safe_load((ROOT / "deploy/compose/prometheus/rules/coire-platform.yml").read_text())
    rules = [rule for group in doc["groups"] for rule in group["rules"]]
    assert {rule["alert"] for rule in rules} == {
        "CoireNodeUnreachable",
        "CoireNodeDegraded",
        "CoireNodeSaturation",
        "CoireThermalThrottling",
        "CoireInterconnectPeerDown",
        "CoireInterconnectFlapping",
        "CoireMemoryLedgerDrift",
        "CoireModelLoadSlow",
        "CoireAgentRunOvertime",
        "CoireTunnelDown",
        "CoireClockSkew",
    }
    for rule in rules:
        assert "subject" in rule["labels"]
        assert {"runbook_url", "dashboard"} <= rule["annotations"].keys()


def test_every_alert_family_has_an_induced_promtool_case() -> None:
    doc = yaml.safe_load(
        (ROOT / "deploy/compose/prometheus/rules/coire-platform.test.yml").read_text()
    )
    tested = {
        case["alertname"] for scenario in doc["tests"] for case in scenario["alert_rule_test"]
    }
    rules = yaml.safe_load(
        (ROOT / "deploy/compose/prometheus/rules/coire-platform.yml").read_text()
    )
    expected = {rule["alert"] for group in rules["groups"] for rule in group["rules"]}
    assert tested == expected


def test_alertmanager_groups_ongoing_conditions() -> None:
    doc = yaml.safe_load((ROOT / "deploy/compose/alertmanager/alertmanager.yml").read_text())
    assert doc["route"]["group_by"] == ["alertname", "cluster", "subject"]
    assert doc["route"]["repeat_interval"] == "4h"


def test_dashboards_are_provisioned_and_link_to_details() -> None:
    directory = ROOT / "deploy/compose/grafana/provisioning/dashboards"
    expected = {"coire-cluster", "coire-traffic", "coire-jobs"}
    dashboards = [
        json.loads((directory / name).read_text())
        for name in ("cluster.json", "traffic.json", "jobs.json")
    ]
    assert {dashboard["uid"] for dashboard in dashboards} == expected
    for dashboard in dashboards:
        assert dashboard["panels"]
        assert any(panel.get("links") for panel in dashboard["panels"])


def test_collector_has_only_local_exporters_and_all_signals() -> None:
    doc = yaml.safe_load((ROOT / "deploy/compose/otel-collector.yaml").read_text())
    assert set(doc["service"]["pipelines"]) == {"traces", "metrics", "logs"}
    rendered = json.dumps(doc["exporters"])
    assert "tempo:" in rendered and "loki:" in rendered
    assert "https://" not in rendered
    redaction = doc["processors"]["attributes/redact"]["actions"]
    assert any(
        action.get("action") == "delete" and "authorization" in action["pattern"]
        for action in redaction
    )
    assert "attributes/redact" in doc["service"]["pipelines"]["traces"]["processors"]
    assert "attributes/redact" in doc["service"]["pipelines"]["logs"]["processors"]


def test_seven_day_ingestion_ceiling_fits_core_disk_budget() -> None:
    """SC-009: default worst-case signal storage leaves ample room for the control plane."""
    seconds = 7 * 24 * 60 * 60
    prometheus_bytes = 32_000_000_000
    loki_bytes = int(0.05 * 1_000_000 * seconds)
    tempo_bytes = 50_000 * seconds
    allocated = prometheus_bytes + loki_bytes + tempo_bytes
    assert allocated == 92_480_000_000
    assert allocated < 100_000_000_000

    compose = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    command = compose["services"]["prometheus"]["command"]
    assert any("retention.size=${COIRE_METRIC_RETENTION_SIZE:-32GB}" in arg for arg in command)

    loki = (ROOT / "deploy/compose/loki/loki.yml").read_text()
    tempo = (ROOT / "deploy/compose/tempo/tempo.yml").read_text()
    assert "COIRE_LOG_INGESTION_MBPS:-0.05" in loki
    assert "COIRE_TRACE_INGESTION_BPS:-50000" in tempo
