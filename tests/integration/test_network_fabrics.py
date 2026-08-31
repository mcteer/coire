"""Separated-fabric topology assertions runnable without physical network mutation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
OVERLAY = ROOT / "deploy/compose/compose.override.it.yaml"


class ComposeLoader(yaml.SafeLoader):
    """Safe loader with Compose's sequence-replacement tag."""


ComposeLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_sequence(node, deep=True)
)


def _compose() -> dict[str, object]:
    loaded = yaml.load(OVERLAY.read_text(), Loader=ComposeLoader)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.integration
def test_core_has_control_but_no_data_attachment() -> None:
    compose = _compose()
    api_networks = compose["services"]["coire-api"]["networks"]
    assert "coire-control-sim" in api_networks
    assert "coire-data-sim" not in api_networks


@pytest.mark.integration
def test_each_node_has_an_independent_control_attachment() -> None:
    compose = _compose()
    for node in ("node-a", "node-b"):
        networks = compose["services"][node]["networks"]
        assert "coire-control-sim" in networks
        assert "coire-data-sim" in networks
        aliases = networks["coire-control-sim"]["aliases"]
        assert f"coire-edge-{node[-1]}.lab" in aliases


@pytest.mark.integration
def test_data_network_is_internal_and_studio_only() -> None:
    compose = _compose()
    assert compose["networks"]["coire-data-sim"]["internal"] is True
    attached = {
        service
        for service, config in compose["services"].items()
        if "coire-data-sim" in (config.get("networks") or {})
    }
    assert attached == {"node-a", "node-b"}


@pytest.mark.integration
def test_replication_names_resolve_only_on_the_data_attachment() -> None:
    compose = _compose()
    for node in ("node-a", "node-b"):
        hosts = compose["services"][node]["extra_hosts"]
        assert all(".fabric:" in entry for entry in hosts)
        assert not any(".mesh" in entry for entry in hosts)
    assert "extra_hosts" not in compose["services"]["coire-api"]
