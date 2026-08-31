"""Separated-fabric topology assertions runnable without physical network mutation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
OVERLAY = ROOT / "deploy/compose/compose.override.it.yaml"


@pytest.mark.integration
def test_core_has_control_but_no_data_attachment() -> None:
    compose = yaml.safe_load(OVERLAY.read_text())
    api_networks = compose["services"]["coire-api"]["networks"]
    assert "coire-control-sim" in api_networks
    assert "coire-data-sim" not in api_networks


@pytest.mark.integration
def test_each_node_has_an_independent_control_attachment() -> None:
    compose = yaml.safe_load(OVERLAY.read_text())
    for node in ("node-a", "node-b"):
        networks = compose["services"][node]["networks"]
        assert "coire-control-sim" in networks
        assert "coire-data-sim" in networks


@pytest.mark.integration
def test_data_network_is_internal_and_studio_only() -> None:
    compose = yaml.safe_load(OVERLAY.read_text())
    assert compose["networks"]["coire-data-sim"]["internal"] is True
    attached = {
        service
        for service, config in compose["services"].items()
        if "coire-data-sim" in (config.get("networks") or {})
    }
    assert attached == {"node-a", "node-b"}


@pytest.mark.integration
def test_replication_names_resolve_only_on_the_data_attachment() -> None:
    compose = yaml.safe_load(OVERLAY.read_text())
    for node in ("node-a", "node-b"):
        hosts = compose["services"][node]["extra_hosts"]
        assert all(".fabric:" in entry for entry in hosts)
        assert not any(".mesh" in entry for entry in hosts)
    assert "extra_hosts" not in compose["services"]["coire-api"]
