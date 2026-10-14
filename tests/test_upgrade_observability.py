"""Upgrade recovery observations are registered independently of diagnostics mode."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_upgrade_rules_and_dashboard_are_actually_provisioned() -> None:
    prometheus = (ROOT / "deploy/compose/prometheus.Dockerfile").read_text()
    grafana = (ROOT / "deploy/compose/grafana.Dockerfile").read_text()
    config = yaml.safe_load((ROOT / "deploy/compose/prometheus/prometheus.yml").read_text())
    compose = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    assert (
        "COPY deploy/observability/alerts/upgrades.yaml /etc/prometheus/rules/upgrades.yml"
        in prometheus
    )
    assert "COPY deploy/observability/grafana/dashboards/upgrades.json" in grafana
    assert "/etc/prometheus/rules/*.yml" in config["rule_files"]
    for service in ("otel-collector", "prometheus", "alertmanager"):
        assert "profiles" not in compose["services"][service]


def test_upgrade_queries_omit_private_identity_and_have_recovery_guidance() -> None:
    rules = yaml.safe_load((ROOT / "deploy/observability/alerts/upgrades.yaml").read_text())[
        "groups"
    ][0]["rules"]
    dashboard = json.loads(
        (ROOT / "deploy/observability/grafana/dashboards/upgrades.json").read_text()
    )
    runbook = ROOT / "docs/runbooks/upgrades.md"
    queries = [rule["expr"] for rule in rules]
    queries.extend(target["expr"] for panel in dashboard["panels"] for target in panel["targets"])
    assert dashboard["uid"] == "coire-upgrades"
    assert len({rule["alert"] for rule in rules}) == len(rules)
    assert runbook.exists()
    for rule in rules:
        anchor = rule["annotations"]["runbook_url"].split("#")[1]
        assert "## " + anchor.capitalize() in runbook.read_text()
    for query in queries:
        assert "coire_upgrade" in query
        assert not any(
            field in query for field in ("job_id", "user_id", "model_id", "prompt", "token=")
        )
