"""Dashboard and alert wiring for the native Chat failure domains."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


def test_chat_dashboard_and_alerts_cover_parser_file_and_vision() -> None:
    root = Path(__file__).parents[1] / "deploy/observability"
    dashboard = json.loads((root / "grafana/dashboards/chat.json").read_text())
    panels = dashboard["panels"]
    assert len({panel["id"] for panel in panels}) == len(panels)
    expressions = "\n".join(target["expr"] for panel in panels for target in panel["targets"])
    for metric in (
        "coire_chat_parser_failures_total",
        "coire_file_processing_total",
        "coire_engine_load_seconds_bucket",
    ):
        assert metric in expressions
    assert 'backend="mlx_vlm"' in expressions
    rules = yaml.safe_load((root / "alerts/chat.yaml").read_text())
    names = {rule["alert"] for group in rules["groups"] for rule in group["rules"]}
    assert {"CoireChatParserFailures", "CoireChatFileProcessingFailures"} <= names
