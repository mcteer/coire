from __future__ import annotations

from pathlib import Path

import yaml


def test_network_alerts_cover_each_failure_domain() -> None:
    path = Path(__file__).parents[1] / "deploy/observability/alerts/network-fabrics.yaml"
    document = yaml.safe_load(path.read_text())
    names = {rule["alert"] for group in document["groups"] for rule in group["rules"]}
    assert names == {
        "CoireControlPathDown",
        "CoireDataLinkDown",
        "CoireForbiddenCrossFabricTraffic",
    }
